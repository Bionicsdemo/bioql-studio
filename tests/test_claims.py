"""The claim-consistency checker. Proven on the real case first: a guard that has never been seen red is worth
nothing (the phrasing is BioQL's own, tests/unit/test_fabrication_guard.py)."""
import pytest

from studio import claims

# The real payload of 2026-11-19, /api/pipeline/admet_standalone, job 5a362e56-077 — before the owner's fix.
REAL_BEFORE = {
    "pipeline": "ADMET_STANDALONE", "quantum_backend": "NONE", "quantum_execution": "CLASSICAL_FALLBACK",
    "ibm_job_ids": [], "molecules_analyzed": 1,
    "summary": {"classical_fallback": 0, "quantum_success": 1, "total_analyzed": 1},
    "results": [{"molecule_id": 1, "status": "SUCCESS", "method": "bionics-admet-lgbm-ensemble",
                 "admet_classical": {"backend": "Bionics LightGBM (classical ML)",
                                     "quantum_engine": "classical LightGBM ensemble (no quantum hardware in this prediction)"},
                 "admet_quantum": {"backend": "Bionics LightGBM (classical ML)",
                                   "quantum_engine": "classical LightGBM ensemble (no quantum hardware in this prediction)"}}],
}

# The same payload after the owner's fix of 2026-11-19: quantum_success derived from ran_quantum, the old count moved
# to predictions_succeeded, classical_only added, ran_quantum and engine per molecule.
REAL_AFTER = {
    "pipeline": "ADMET_STANDALONE", "quantum_backend": "NONE", "quantum_execution": "CLASSICAL_FALLBACK",
    "ibm_job_ids": [], "molecules_analyzed": 1,
    "summary": {"quantum_success": 0, "predictions_succeeded": 1, "classical_only": 1, "total_analyzed": 1},
    "results": [{"molecule_id": 1, "status": "SUCCESS", "ran_quantum": False, "engine": "classical ML",
                 "admet_classical": {"backend": "Bionics LightGBM (classical ML)"},
                 "admet_quantum": {"backend": "Bionics LightGBM (classical ML)"}}],
}


def test_the_real_case_before_the_fix_is_caught():
    f = claims.check_claims(REAL_BEFORE, "job 5a362e56-077")
    assert claims.verdict(f) == "contradicted"
    hits = {x["field"] for x in f if x["code"] == "claim_without_execution"}
    assert "$.summary.quantum_success" in hits, hits
    assert any(x["payload"] == "job 5a362e56-077" for x in f)      # it names the payload, as asked
    assert all(x["field"].startswith("$") for x in f)              # and it names the field
    said = [x for x in f if x["field"] == "$.summary.quantum_success"][0]["detail"]
    assert "quantum_backend" not in said or "NONE" in said or "[]" in said


def test_the_owners_fix_resolves_the_counter():
    """quantum_success: 0 is a denial, so the counter is no longer a claim. predictions_succeeded and
    classical_only carry no quantum word, so they are not claims either."""
    f = claims.check_claims(REAL_AFTER, "after the fix")
    bad = {x["field"] for x in f if x["code"] == "claim_without_execution"}
    assert "$.summary.quantum_success" not in bad
    assert "$.summary.predictions_succeeded" not in bad
    assert "$.summary.classical_only" not in bad


def test_the_fix_the_owner_first_proposed_would_not_have_resolved_it():
    """Their first idea was `sum(1 for r in results if r.get("status") == "SUCCESS")`, which still counts a classical
    run as a quantum success. The checker must still object — this is what keeps the field honest."""
    half = {**REAL_BEFORE, "summary": {"classical_fallback": 0, "quantum_success": 1, "total_analyzed": 1}}
    f = claims.check_claims(half, "first proposal")
    assert "$.summary.quantum_success" in {x["field"] for x in f if x["code"] == "claim_without_execution"}


def test_an_honest_denial_is_never_flagged():
    honest = {"quantum_backend": "NONE", "quantum_execution": "CLASSICAL_FALLBACK", "ibm_job_ids": [],
              "quantum_engine": "classical LightGBM ensemble (no quantum hardware in this prediction)",
              "summary": {"quantum_success": 0, "classical_fallback": 1},
              "is_real_hardware": False, "ran_quantum": False}
    assert claims.check_claims(honest) == []
    assert claims.verdict([]) == "consistent"


def test_a_real_quantum_run_is_never_flagged():
    real = {"quantum_backend": "ibm_marrakesh", "ibm_job_ids": ["dbimbpo1h92s7383phog"], "is_real_hardware": True,
            "quantum_energy": -75.12, "quantum_success": 1, "qpu_seconds": 42}
    assert claims.check_claims(real) == []
    assert claims.execution_record(real)["ran"] is True


def test_a_simulator_run_counts_as_quantum_execution():
    sim = {"quantum_backend": "aer_simulator", "ibm_job_ids": [], "is_real_hardware": False, "quantum_energy": -1.1}
    assert claims.check_claims(sim) == []
    assert claims.execution_record(sim)["ran"] is True


def test_no_record_at_all_is_unverifiable_not_false():
    """A missing record is not a denial. Degrading 'cannot tell' into 'did not happen' is the defect the three-state
    invariant exists to stop; here the same discipline applies to names."""
    vague = {"quantum_advantage_ratio": 1.4, "score": 0.9}
    f = claims.check_claims(vague)
    assert claims.verdict(f) == "unverifiable"
    assert [x["code"] for x in f] == ["claim_unverifiable"]
    assert claims.execution_record(vague)["ran"] is None


def test_a_value_that_asserts_under_an_innocent_key():
    """A DATA field whose value names a device, even under a harmless key name."""
    sneaky = {"quantum_backend": "NONE", "ibm_job_ids": [], "execution_lane": "executed on ibm_fez with 4096 shots"}
    assert [x["field"] for x in claims.check_claims(sneaky)] == ["$.execution_lane"]


def test_known_limit_a_false_claim_buried_in_prose_is_not_caught():
    """Stated, not hidden. Prose fields are skipped because the honest ones discuss the ABSENCE of quantum work and
    flagging them buries the real findings. The cost is that a false claim written into a `note` passes. Catching
    that needs a reader, or a check narrow enough to tell a warning from a boast — this one is not."""
    buried = {"quantum_backend": "NONE", "ibm_job_ids": [], "note": "executed on ibm_fez with 4096 shots"}
    assert claims.check_claims(buried) == []


def test_keys_are_matched_by_whole_word():
    assert claims.check_claims({"quantum_backend": "NONE", "ibm_job_ids": [], "equantumx": 5}) == []
    assert claims.check_claims({"quantum_backend": "NONE", "ibm_job_ids": [], "hardwarez": 5}) == []
    assert [x["field"] for x in claims.check_claims({"quantum_backend": "NONE", "ibm_job_ids": [],
                                                      "qpu_calls": 5})] == ["$.qpu_calls"]


def test_a_populated_quantum_named_result_over_classical_work():
    f = claims.check_claims(REAL_BEFORE)
    assert any(x["field"].endswith("admet_quantum") for x in f)


def test_the_record_reports_mixed_hardware_claims():
    rec = claims.execution_record({"a": {"is_real_hardware": True}, "b": {"is_real_hardware": False}})
    assert rec["is_real_hardware"] == "mixed" and rec["ran"] is True


@pytest.mark.parametrize("deny", ["NONE", "none", "n/a", "unspecified", "CLASSICAL_FALLBACK", "classical ML",
                                  "Bionics LightGBM (classical ML)", "mock engine", "cpu-only", "-", "0"])
def test_every_denial_is_read_as_a_denial(deny):
    """Including the ones whose honest word is not at the start, and the ones joined by an underscore."""
    assert claims.execution_record({"quantum_backend": deny})["ran"] is False


@pytest.mark.parametrize("named", ["aer_simulator", "statevector", "local qiskit-aer MPS", "cudaq_nvidia_A100",
                                   "Modal CUDA-Q (A100)", "ibm_fez", "vqe"])
def test_a_named_quantum_lane_counts_as_execution(named):
    assert claims.execution_record({"quantum_backend": named})["ran"] is True


@pytest.mark.parametrize("vague", ["simulated", "simulator", "some-new-engine-v2", "internal"])
def test_an_unrecognised_backend_is_unknown_not_a_denial(vague):
    """A bare "simulated" does not say which engine ran. Calling that False would be inventing a denial; calling it
    True would be inventing an execution. It is neither."""
    assert claims.execution_record({"quantum_backend": vague})["ran"] is None


def test_a_request_is_not_evidence_of_a_run():
    """`input.run_quantum: true` is what was asked for. Counting it as a contradiction would cry wolf; ignoring it
    would hide that a reader will misread it. It gets its own code."""
    asked = {"quantum_backend": "NONE", "ibm_job_ids": [], "input": {"run_quantum": True}}
    f = claims.check_claims(asked)
    assert [x["code"] for x in f] == ["claim_in_request"]
    assert claims.verdict(f) == "requested_only"
    assert "ASKED FOR" in f[0]["detail"]


def test_a_request_subtree_does_not_excuse_the_result_fields():
    both = {"quantum_backend": "NONE", "ibm_job_ids": [], "input": {"run_quantum": True}, "quantum_success": 1}
    f = claims.check_claims(both)
    assert {x["code"] for x in f} == {"claim_in_request", "claim_without_execution"}
    assert claims.verdict(f) == "contradicted"


MRNA_REAL = {   # the real shape of ~/.bioql/artifacts/ps-96bebd3b336c/result.json (mrna_therapeutic_v1)
    "pipeline": "mrna_therapeutic_v1", "quantum_structure_validated": True,
    "input": {"run_quantum": True},
    "stages": {
        "4_mrna_optimization": {"quantum_sequence_state": {"backend": "local qiskit-aer MPS", "ok": True}},
        "6_quantum_cascade": {
            "stage": "Quantum Algorithm Cascade (Modal CUDA-Q (A100))", "backend": "Modal CUDA-Q (A100)",
            "backend_note": "Label derived from the lane each algorithm actually executed on.",
            "algorithms": {"QAOA": {"status": "success", "backend": "cudaq_nvidia_A100", "ibm_job_id": None}},
            "quantum_compute": {"success": True, "backend": "vqe", "n_qubits": 12},
            "ewf_sqd": {"energy": -1.2}}},
}


def test_a_pipeline_that_records_its_lane_under_plain_backend_is_consistent():
    """mrna_therapeutic writes `cudaq_nvidia_A100` and `local qiskit-aer MPS` under the plain key `backend`, nested
    in its stages. An earlier version of this checker read only `quantum_backend` and called 53 honest payloads
    contradicted. This test is that blind spot, nailed down."""
    f = claims.check_claims(MRNA_REAL, "ps-96bebd3b336c")
    assert claims.execution_record(MRNA_REAL)["ran"] is True
    assert [x for x in f if x["severity"] == "high"] == []
    assert claims.verdict(f) == "consistent"


def test_a_classical_backend_under_the_plain_key_still_denies():
    """Widening the record must not blunt it: `backend: "Bionics LightGBM (classical ML)"` is still a denial."""
    assert claims.execution_record(REAL_BEFORE)["ran"] is False
    assert claims.verdict(claims.check_claims(REAL_BEFORE)) == "contradicted"


def test_an_honest_disclaimer_is_not_a_claim():
    """BioQL's own ADMET report says "No IBM hardware job id present; any quantum-hardware claim is NOT supported by
    this run" and carries a regulatory disclaimer naming quantum hardware. An earlier version of this checker
    flagged both — it read the warning as the thing warned about."""
    honest = {"quantum_backend": "NONE", "ibm_job_ids": [],
              "limitations": ["No IBM hardware job id present; any quantum-hardware claim is NOT supported by this run"],
              "regulatory_disclaimer": "IN-SILICO RESEARCH OUTPUT. Quantum hardware was not used.",
              "note": "executed on ibm_fez" }
    assert claims.check_claims(honest) == []


def test_skipping_prose_does_not_excuse_a_data_field():
    mixed = {"quantum_backend": "NONE", "ibm_job_ids": [],
             "note": "no quantum hardware here", "quantum_success": 1}
    assert [x["field"] for x in claims.check_claims(mixed)] == ["$.quantum_success"]
