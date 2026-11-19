"""Rails and evals. Two things have to be true for this module to be worth anything:

the promotion must refuse to fire on a single rejection (or a rail means nothing), and the eval suite must be able
to FAIL (a suite that always passes is the same defect it prevents — CLAUDE.md, 2026-09-18). Both are tested here by
breaking something on purpose and watching the failure arrive.
"""
import json

import pytest

from studio import rails, verify
from studio.adapter import provenance
from studio.resolution import Unresolvable


# --- the store


def test_no_rails_file_is_absent_not_a_failure():
    """ABSENT is information: the source answered, there are no rails yet."""
    res = rails.read_store()
    assert res.state == "ABSENT" and res.or_default({"rails": {}})["rails"] == {}
    assert rails.list_rails() == [] and rails.block([{"code": "x", "severity": "high"}]) == []


def test_a_corrupt_rails_file_is_unresolvable_and_never_reads_as_no_rails():
    rails.rails_path().write_text("{not json")
    res = rails.read_store()
    assert res.state == "UNRESOLVABLE"
    with pytest.raises(Unresolvable):
        res.or_default({"rails": {}})
    for call in (rails.list_rails, lambda: rails.block([{"code": "x", "severity": "high"}])):
        with pytest.raises(Unresolvable):
            call()


def test_a_rail_that_could_not_be_applied_is_a_corrupt_store():
    """Not "a rail with a defect": a store that cannot be applied as written. Hand-edited into every shape that
    would otherwise crash somewhere later, far from the file that caused it."""
    rail = {"code": "c", "severity": "high", "why": "w"}
    for bad in ({"rails": {"c": {**rail, "pattern": "(unclosed"}}},
                {"rails": {"c": {**rail, "pattern": 7}}},
                {"rails": {"c": {**rail, "why": ""}}},
                {"rails": {"c": {**rail, "severity": "blocking"}}},
                {"rails": {"c": {**rail, "code": "other"}}},
                {"rails": {"c": "a rail written as a string"}},
                {"rails": {}, "rejections": {"c": 3}},
                {"rails": {}, "events": "not a list"},
                "a rails file that is not an object"):
        rails.rails_path().write_text(json.dumps(bad))
        assert rails.read_store().state == "UNRESOLVABLE", bad


def test_an_unfamiliar_severity_blocks_rather_than_slips_through():
    """A rail fails closed: a finding whose severity this version does not know is not silently under every floor."""
    rail = [{"code": "c", "severity": verify.HIGH, "why": "w", "pattern": None}]
    assert len(rails.block([{"code": "c", "severity": "blocking", "detail": "d", "path": "$"}], rail)) == 1


def test_a_write_never_overwrites_a_store_it_could_not_read():
    rails.rails_path().write_text("{not json")
    before = rails.rails_path().read_text()
    for call in (lambda: rails.add_rail("c", "why"), lambda: rails.note_rejection("c", "d"),
                 lambda: rails.remove_rail("c"), lambda: rails.promote("c")):
        with pytest.raises(Unresolvable):
            call()
    assert rails.rails_path().read_text() == before


def test_the_store_is_private_and_atomic():
    rails.add_rail("quantum_twin", "the same work under two names was rejected twice")
    assert oct(rails.rails_path().stat().st_mode)[-3:] == "600"
    assert not list(rails.rails_path().parent.glob("rails.json.tmp"))


# --- rails block a report


def test_add_list_remove():
    rails.add_rail("quantum_twin", "no report while a _quantum field holds its _classical twin", severity=verify.HIGH)
    assert [r["code"] for r in rails.list_rails()] == ["quantum_twin"]
    assert rails.rail_for("quantum_twin")["severity"] == verify.HIGH
    assert rails.remove_rail("quantum_twin") is True
    assert rails.list_rails() == [] and rails.remove_rail("quantum_twin") is False


def test_add_rail_refuses_a_rail_nobody_could_read():
    for bad in (lambda: rails.add_rail("", "why"), lambda: rails.add_rail("c", ""),
                lambda: rails.add_rail("c", "why", severity="blocking"),
                lambda: rails.add_rail("c", "why", pattern="(unclosed")):
        with pytest.raises(ValueError):
            bad()


def test_block_is_a_floor_not_an_equality():
    """A rail on a warn code must block that warning, or promoting a repeated `weak_arm_missing` rejection would
    build a rail that can never fire."""
    warn_rail = [{"code": "weak_arm_missing", "severity": verify.WARN, "why": "w", "pattern": None}]
    high_rail = [{"code": "weak_arm_missing", "severity": verify.HIGH, "why": "w", "pattern": None}]
    warning = [{"code": "weak_arm_missing", "severity": verify.WARN, "detail": "d", "path": "$"}]
    assert len(rails.block(warning, warn_rail)) == 1
    assert rails.block(warning, high_rail) == []                 # a high-only rail ignores a warning
    assert len(rails.block([{**warning[0], "severity": verify.HIGH}], high_rail)) == 1


def test_block_is_pure_and_an_empty_rail_list_is_not_the_store():
    rails.add_rail("quantum_twin", "blocked")
    findings = [{"code": "quantum_twin", "severity": verify.HIGH, "detail": "d", "path": "$"}]
    assert rails.block(findings, []) == []                       # [] means no rails, it does not mean "read the file"
    out = rails.block(findings)
    assert len(out) == 1 and out[0]["rail"] == "quantum_twin"
    assert findings == [{"code": "quantum_twin", "severity": verify.HIGH, "detail": "d", "path": "$"}]
    assert rails.rail_for("quantum_twin")["hits"] == 0           # block() reads, it never writes


def test_a_pattern_narrows_a_rail_to_one_field():
    rails.add_rail("quantum_twin", "only the ADMET twin", pattern=r"admet")
    hit = [{"code": "quantum_twin", "severity": verify.HIGH, "detail": "'admet_quantum' and its twin", "path": "$.r"}]
    miss = [{"code": "quantum_twin", "severity": verify.HIGH, "detail": "'docking_quantum' and its twin", "path": "$.r"}]
    assert len(rails.block(hit)) == 1 and rails.block(miss) == []


def test_gate_blocks_the_report_and_counts_only_what_it_read_itself():
    findings = verify.check(rails.REAL_ADMET, provenance(rails.REAL_ADMET))
    open_gate = rails.gate(findings)
    assert open_gate["allowed"] is True and open_gate["verdict"] == "contradicted"
    assert open_gate["blocked_by"] == []                         # contradicted, and still reportable: no rail yet

    rails.add_rail("quantum_twin", "the owner rejected this twice", severity=verify.HIGH)
    shut = rails.gate(findings)
    assert shut["allowed"] is False and shut["verdict"] == "contradicted"
    assert [b["code"] for b in shut["blocked_by"]] == ["quantum_twin"]
    assert rails.rail_for("quantum_twin")["hits"] == 1
    rails.gate(findings, rails=rails.list_rails())               # rails passed in: a pure query
    assert rails.rail_for("quantum_twin")["hits"] == 1


def test_an_explicit_store_file_is_the_only_store_the_gate_touches(tmp_path):
    """One store per call: reading the rails from one file while counting the hit in another would make a rail that
    blocks nothing and a count nobody can explain."""
    other = tmp_path / "other-rails.json"
    rails.add_rail("quantum_twin", "the default store's rail", severity=verify.HIGH)
    rails.add_rail("counter_vs_execution", "the other store's rail", severity=verify.HIGH, file=other)
    findings = verify.check(rails.REAL_ADMET, provenance(rails.REAL_ADMET))
    g = rails.gate(findings, file=other)
    assert [b["code"] for b in g["blocked_by"]] == ["counter_vs_execution"]
    assert rails.rail_for("counter_vs_execution", file=other)["hits"] == 1
    assert rails.rail_for("quantum_twin")["hits"] == 0


def test_a_rail_never_changes_a_number_and_never_hides_a_finding():
    """It blocks a REPORT, not a run: the findings and the result come out of the gate as they went in."""
    before = json.dumps(rails.REAL_ADMET, sort_keys=True)
    findings = verify.check(rails.REAL_ADMET, provenance(rails.REAL_ADMET))
    rails.add_rail("counter_vs_execution", "rejected twice", severity=verify.HIGH)
    g = rails.gate(findings)
    assert not g["allowed"]
    assert len(findings) == 3 and {f["code"] for f in findings} == {
        "quantum_label_vs_execution", "quantum_twin", "counter_vs_execution"}
    assert json.dumps(rails.REAL_ADMET, sort_keys=True) == before


# --- promotion from repetition


def test_one_rejection_never_promotes():
    out = rails.note_rejection("counter_vs_execution", "the summary counts a quantum success for a classical run")
    assert out["rejections"] == 1 and out["promoted"] is False and out["rail"] is None
    assert rails.list_rails() == [] and rails.promote("counter_vs_execution") is None


def test_the_same_rejection_twice_becomes_a_rail_with_its_history():
    rails.note_rejection("counter_vs_execution", "counts a classical run as a quantum success", by="Heinz")
    out = rails.note_rejection("counter_vs_execution", "again: still counting SUCCESS as quantum", by="the auditor")
    assert out["promoted"] is True
    rail = rails.rail_for("counter_vs_execution")
    assert rail["source"] == "promoted after 2 rejections" and len(rail["history"]) == 2
    assert "the auditor" in rail["why"] and "Heinz" in rail["why"]
    events = [e["event"] for e in rails.history("counter_vs_execution")]
    assert events == ["rejected", "rejected", "promoted"]        # a person can read why the rail exists


def test_promote_is_idempotent():
    rails.note_rejection("quantum_twin", "a")
    rails.note_rejection("quantum_twin", "b")
    first = rails.rail_for("quantum_twin")
    for _ in range(3):
        assert rails.promote("quantum_twin") == first
    rails.note_rejection("quantum_twin", "c")
    assert rails.rail_for("quantum_twin") == first                # unchanged, and no second promotion event
    assert [e["event"] for e in rails.history("quantum_twin")].count("promoted") == 1


def test_a_threshold_below_two_is_refused():
    for t in (1, 0, -1):
        with pytest.raises(ValueError):
            rails.note_rejection("c", "d", threshold=t)
        with pytest.raises(ValueError):
            rails.promote("c", threshold=t)


def test_a_higher_threshold_holds():
    for i in range(3):
        out = rails.note_rejection("weak_arm_missing", f"r{i}", threshold=4)
        assert out["promoted"] is False
    assert rails.note_rejection("weak_arm_missing", "r3", threshold=4)["promoted"] is True


def test_un_promoting_does_not_re_promote_on_the_next_rejection():
    """The old rejections still meet the threshold; if they kept counting, removing a rail would last one sentence."""
    rails.note_rejection("quantum_twin", "a")
    rails.note_rejection("quantum_twin", "b")
    assert rails.remove_rail("quantum_twin", why="the owner decided this twin is intended here")
    assert rails.promote("quantum_twin") is None
    assert rails.note_rejection("quantum_twin", "c")["promoted"] is False
    assert rails.note_rejection("quantum_twin", "d")["promoted"] is True        # two NEW ones promote again
    assert len(rails.rail_for("quantum_twin")["history"]) == 2
    assert len(rails.history("quantum_twin")) == 7                              # the whole story is kept


# --- the evals


def test_the_builtin_suite_passes_and_covers_the_real_run():
    r = rails.run_evals()
    assert r["ok"] is True and r["rate"] == 1.0 and r["total"] >= 4
    assert r["failed"] == 0
    by = {c["name"]: c for c in r["cases"]}
    assert by["real_admet_envelope_2026_11_19"]["verdict"] == "contradicted"
    assert set(by["real_admet_envelope_2026_11_19"]["codes"]) == {
        "quantum_label_vs_execution", "quantum_twin", "counter_vs_execution"}
    assert by["honest_classical_admet"]["codes"] == []
    assert by["hardware_claim_without_a_job_id"]["codes"] == ["hardware_without_job"]


def test_the_owners_proposed_fix_does_not_resolve_the_contradiction():
    """They intend `sum(1 for r in results["results"] if r.get("status") == "SUCCESS")`. It counts a row that
    finished, not a row that ran on a quantum backend, so a classical row still counts as a quantum success."""
    fixed = rails.owners_proposed_fix()
    assert fixed["summary"]["quantum_success"] == sum(
        1 for r in rails.REAL_ADMET["results"] if r.get("status") == "SUCCESS")
    assert fixed["summary"]["quantum_success"] == 1 and fixed["summary"]["classical_fallback"] == 0
    assert fixed["summary"] == rails.REAL_ADMET["summary"]        # on this run the fix is literally a no-op
    codes = {f["code"] for f in verify.check(fixed, provenance(fixed))}
    assert "counter_vs_execution" in codes
    case = next(c for c in rails.BUILTIN if c["name"] == "owners_proposed_counter_fix")
    assert rails.run_case(case)["passed"] is True


def test_counting_by_execution_instead_is_what_clears_the_check():
    """The contrast that shows the eval is not just asserting the verifier is noisy: counters that match the
    execution lose `counter_vs_execution`, while the owner's version keeps it."""
    honest = json.loads(json.dumps(rails.REAL_ADMET))
    honest["summary"] = {**honest["summary"], "quantum_success": 0, "classical_fallback": 1}
    assert "counter_vs_execution" not in {f["code"] for f in verify.check(honest, provenance(honest))}
    assert "counter_vs_execution" in {f["code"] for f in verify.check(rails.owners_proposed_fix(),
                                                                      provenance(rails.owners_proposed_fix()))}


def test_a_broken_fixture_makes_the_suite_fail():
    """A suite that cannot fail is worthless. The real case's fixture is replaced by the honest one while its
    expectations stay: the verifier goes quiet, so the case must go red."""
    case = next(dict(c) for c in rails.BUILTIN if c["name"] == "real_admet_envelope_2026_11_19")
    case["result_fixture"] = rails.HONEST_ADMET
    row = rails.run_case(case)
    assert row["passed"] is False and row["verdict"] == "clean"
    assert row["missing_codes"] == ["counter_vs_execution", "quantum_label_vs_execution", "quantum_twin"]
    r = rails.run_evals(cases=rails.BUILTIN + [case])
    assert r["ok"] is False and r["failed"] == 1 and r["rate"] < 1.0


def test_a_partially_broken_fixture_fails_on_the_missing_code_alone():
    """Only the COUNTER is repaired — which is exactly the partial repair the owner made on 2026-11-19: derive
    quantum_success from what ran, and keep `admet_quantum` for compatibility. The twin stays, so the verdict is
    still `contradicted` and a case that checked only the verdict would pass. The missing code is what catches it."""
    half = json.loads(json.dumps(rails.REAL_ADMET))
    half["summary"] = {"quantum_success": 0, "classical_fallback": 1, "total_analyzed": 1}
    case = next(dict(c) for c in rails.BUILTIN if c["name"] == "real_admet_envelope_2026_11_19")
    case["result_fixture"] = half
    row = rails.run_case(case)
    assert row["verdict"] == "contradicted" and row["passed"] is False
    assert "counter_vs_execution" in row["missing_codes"], row
    assert "quantum_twin" in row["codes"], row


class Hostile(dict):
    """A fixture is data, and data must not be able to take the suite down."""

    def items(self):
        raise RuntimeError("this fixture raises when it is walked")


def test_run_evals_reports_a_failure_rather_than_raising():
    broken = [{"name": "no_fixture_at_all", "expect_verdict": "clean", "expect_codes": []},
              {"name": "fixture_that_explodes", "result_fixture": Hostile(a=1), "expect_verdict": "clean"},
              {"name": "wrong_expectation", "result_fixture": rails.HONEST_ADMET, "expect_verdict": "contradicted",
               "expect_codes": ["counter_vs_execution"]}]
    r = rails.run_evals(cases=broken)
    assert r["failed"] == 3 and r["passed"] == 0 and r["rate"] == 0.0 and r["ok"] is False
    assert all(c["error"] for c in r["cases"])
    assert "KeyError" in r["cases"][0]["error"]


def test_an_empty_suite_is_not_a_hundred_percent():
    r = rails.run_evals(cases=[])
    assert r["total"] == 0 and r["rate"] == 0.0 and r["ok"] is False


def test_a_stored_case_joins_the_suite():
    rails.save_eval({"name": "job_id_while_denying_hardware",
                     "result_fixture": {"is_real_hardware": False, "ibm_job_id": "dbimbpo1h92s7383phog"},
                     "expect_verdict": "contradicted", "expect_codes": ["job_without_hardware"],
                     "why": "provenance against itself, from a real job"})
    assert rails.read_eval("job_id_while_denying_hardware").resolved
    r = rails.run_evals()
    assert r["ok"] is True and "job_id_while_denying_hardware" in {c["name"] for c in r["cases"]}


def test_a_stored_case_is_refused_when_it_cannot_be_a_test():
    with pytest.raises(ValueError):
        rails.save_eval({"name": "real_admet_envelope_2026_11_19", "result_fixture": {},
                         "expect_verdict": "clean"})          # shadowing a built-in
    with pytest.raises(ValueError):
        rails.save_eval({"name": "Not A Name", "result_fixture": {}, "expect_verdict": "clean"})
    with pytest.raises(ValueError):
        rails.save_eval({"name": "no_expectation", "result_fixture": {}})
    assert rails.read_eval("no_expectation").state == "ABSENT"


def test_an_unreadable_stored_case_fails_the_suite_instead_of_shrinking_it():
    """A corrupt case is not one fewer case: that would quietly run a smaller suite and still report 100%."""
    (rails.evals_dir() / "half_written.json").write_text('{"result_fixture":')
    assert rails.read_eval("half_written").state == "UNRESOLVABLE"
    r = rails.run_evals()
    assert r["ok"] is False and r["failed"] == 1
    bad = next(c for c in r["cases"] if c["name"] == "half_written")
    assert "cannot be trusted" in bad["error"]
