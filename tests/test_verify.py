"""The verifier. Each case is a real shape BioQL produced or a real defect the owner's audits found by hand.

A guard that never fires is the same defect it prevents, so the clean cases matter as much as the dirty ones.
"""
import pytest

from studio import verify
from studio.adapter import provenance

REAL_ADMET = {   # the shape of the real run of 2026-11-19, job 5a362e56-077, cut to what the checks read
    "pipeline": "ADMET_STANDALONE", "quantum_backend": "NONE", "quantum_execution": "CLASSICAL_FALLBACK",
    "ibm_job_ids": [], "molecules_analyzed": 1,
    "summary": {"classical_fallback": 0, "quantum_success": 1, "total_analyzed": 1},
    "results": [{"molecule_id": 1, "smiles": "CC(=O)Nc1ccc(O)cc1", "status": "SUCCESS",
                 "method": "bionics-admet-lgbm-ensemble",
                 "admet_classical": {"backend": "Bionics LightGBM (classical ML)", "raw_predictions": {"ames": 0.1337},
                                     "quantum_engine": "classical LightGBM ensemble (no quantum hardware in this prediction)"},
                 "admet_quantum": {"backend": "Bionics LightGBM (classical ML)", "raw_predictions": {"ames": 0.1337},
                                   "quantum_engine": "classical LightGBM ensemble (no quantum hardware in this prediction)"}}],
}


def test_the_real_admet_run_is_contradicted():
    f = verify.check(REAL_ADMET, provenance(REAL_ADMET))
    codes = {x["code"] for x in f}
    assert verify.verdict(f) == "contradicted"
    assert {"quantum_label_vs_execution", "quantum_twin", "counter_vs_execution"} <= codes
    assert all(x["path"].startswith("$") for x in f)


def test_an_honest_classical_result_is_clean():
    """The same work, named for what it is: nothing to report."""
    honest = {"pipeline": "ADMET_STANDALONE", "quantum_backend": "NONE", "quantum_execution": "CLASSICAL_FALLBACK",
              "summary": {"classical_fallback": 1, "quantum_success": 0, "total_analyzed": 1},
              "results": [{"admet_classical": {"backend": "Bionics LightGBM (classical ML)",
                                                "raw_predictions": {"ames": 0.1337}}}]}
    assert verify.check(honest, provenance(honest)) == []
    assert verify.verdict([]) == "clean"


def test_a_plain_local_answer_is_clean():
    for ok in ({"mol": "…sdf…", "smiles": "CCO"}, {"image_base64": "iVBOR", "format": "png"},
               {"categories": {"alkyls": [{"name": "methyl"}]}, "total_motifs": 120}):
        assert verify.check(ok, provenance(ok)) == []


def test_real_hardware_needs_a_job_id():
    bad = {"energy": -1.2, "is_real_hardware": True, "quantum_backend": "ibm_fez"}
    f = verify.check(bad, provenance(bad))
    assert [x["code"] for x in f] == ["hardware_without_job"]
    good = {"energy": -1.2, "is_real_hardware": True, "ibm_job_id": "dbi185ghoj4c73b5d1p0", "quantum_backend": "ibm_fez"}
    assert verify.check(good, provenance(good)) == []


def test_a_job_id_while_denying_hardware():
    bad = {"is_real_hardware": False, "ibm_job_id": "dbimbpo1h92s7383phog"}
    assert [x["code"] for x in verify.check(bad, provenance(bad))] == ["job_without_hardware"]


def test_mixed_hardware_claims_are_flagged():
    mixed = {"a": {"is_real_hardware": True, "ibm_job_id": "d0x"}, "b": {"is_real_hardware": False}}
    assert "hardware_mixed" in {x["code"] for x in verify.check(mixed, provenance(mixed))}


def test_determinant_selection_without_the_subspace_fraction():
    """CLAUDE.md, 2026-10-07: publish dim/dim_FCI or the point cannot tell sampling from full CI."""
    bad = {"sqd_energy": -75.1, "selected_determinants": 4096}
    assert "subspace_fraction_missing" in {x["code"] for x in verify.check(bad, provenance(bad))}
    good = {"sqd_energy": -75.1, "selected_determinants": 4096, "subspace_fraction": 0.03}
    assert "subspace_fraction_missing" not in {x["code"] for x in verify.check(good, provenance(good))}


def test_an_advantage_claim_needs_a_classical_arm():
    bad = {"note": "shows a clear quantum advantage over the classical method", "score": 0.91}
    assert "weak_arm_missing" in {x["code"] for x in verify.check(bad, provenance(bad))}
    good = {"note": "shows a clear quantum advantage over the classical method", "score": 0.91,
            "classical_baseline": {"score": 0.88}}
    assert "weak_arm_missing" not in {x["code"] for x in verify.check(good, provenance(good))}


def test_keys_are_matched_by_token_not_substring():
    """`"kd" in path` also matches `score_breakdown`; that inflated an audit from 60% to 94% (CLAUDE.md)."""
    assert verify._find({"score_breakdown": 1, "kd": 2}, "kd") == [("$.kd", 2)]
    assert verify._find({"subspace_fraction_note": "x"}, "subspace_fraction") == []
    # a key merely CONTAINING a quantum word is not a quantum-named field
    assert verify.check({"quantum_execution": "CLASSICAL_FALLBACK", "equantumx": {"a": 1}},
                        {"is_real_hardware": None, "ibm_job_ids": [], "quantum_backends": []}) == []


def test_the_verifier_runs_on_every_answer(fake_bioql):
    """Wired into the adapter, with no exempt kind: a finished job's science arrives through a *status* route
    (/api/pipeline/job/<id>/result), so exempting status reads would skip the body that matters most."""
    from studio import allowlist as al
    from studio.adapter import Adapter
    ents = {k: al._norm(k, v) for k, v in al.DEFAULTS.items()}
    ents["p"] = al._norm("p", {"method": "POST", "path": "/api/mixed", "kind": "compute"})
    a = Adapter(fake_bioql, ents)
    res = a.call("p", {}, approved_by="a person")
    assert res.meta["verdict"] == "contradicted" and res.meta["findings"]
    assert a.call("backend_health").meta["verdict"] == "clean"
    assert a.call("pipeline_job", {"job_id": "nope"}).meta["verdict"] is None      # ABSENT: nothing to verify
