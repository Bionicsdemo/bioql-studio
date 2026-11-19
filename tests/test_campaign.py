"""The campaign, proved mostly through what it refuses to do. The planner and the auditor are stubs: this module
orchestrates, and no language model is involved in it or in these tests."""
import json
import threading
import time
from pathlib import Path

import pytest

from studio import allowlist as al
from studio import campaign as cm
from studio import ledger
from studio.adapter import Adapter, Refused
from studio.resolution import RESOLVED, UNRESOLVABLE

QUESTION = "Does the 3D structure of ethanol come out of BioQL at all?"


def entries(**extra):
    base = {k: al._norm(k, v) for k, v in al.DEFAULTS.items()}
    base["mixed"] = al._norm("mixed", {"method": "POST", "path": "/api/mixed", "kind": "local"})
    base["dead"] = al._norm("dead", {"method": "POST", "path": "/api/boom", "kind": "local"})
    base["slow"] = al._norm("slow", {"method": "POST", "path": "/api/slow", "kind": "local", "timeout": 30})
    base["heavy"] = al._norm("heavy", {"method": "POST", "path": "/api/smiles-to-3d", "kind": "compute",
                                       "required": ["smiles"]})
    for k, v in extra.items():
        base[k] = al._norm(k, v)
    return base


def ad(url, **extra):
    return Adapter(url, entries(**extra), poll_every=0.05)


def planner(runs, title="A plan", criteria=("ethanol has a structure",)):
    return lambda q: {"title": title, "criteria": list(criteria), "runs": list(runs)}


def run(tool, args=None, why="because", **extra):
    return {"tool": tool, "args": args if args is not None else {}, "why": why, **extra}


def auditor_says(supported=True, why="re-derived from the numbers"):
    def a(ev):
        assert "numbers" not in json.dumps(ev["criteria"])            # the auditor gets data, not a summary
        return [{"claim": c, "supported": supported, "why": why} for c in ev["criteria"]]
    return a


def confirmed(a, runs, **kw):
    c = cm.plan(QUESTION, planner(runs, **kw), adapter=a)
    return cm.confirm(c, "Heinz")


# ------------------------------------------------------------------ 1. the plan is validated, never trimmed
def test_a_tool_outside_the_allowlist_is_rejected(fake_bioql):
    a = ad(fake_bioql)
    with pytest.raises(cm.Rejected) as e:
        cm.plan(QUESTION, planner([run("smiles_to_3d", {"smiles": "CCO"}), run("qpu_on_a_whim")]), adapter=a)
    assert "not in Studio's allowlist" in str(e.value)
    rec = cm.load(e.value.campaign_id)                 # rejected, and recorded with its reason
    assert rec.state == cm.FAILED and "allowlist" in rec.reason and rec.runs == []


def test_a_smuggled_argument_is_rejected(fake_bioql):
    a = ad(fake_bioql)
    with pytest.raises(cm.Rejected) as e:
        cm.plan(QUESTION, planner([run("smiles_to_3d", {"smiles": "CCO", "real_qpu": True})]), adapter=a)
    assert "does not take ['real_qpu']" in str(e.value)
    with pytest.raises(cm.Rejected):                   # and a missing required argument is not filled in for it
        cm.plan(QUESTION, planner([run("smiles_to_3d", {})]), adapter=a)


def test_a_plan_cannot_carry_its_own_approval(fake_bioql):
    """`approved_by` is a person's, not a field of a plan: the planner naming one is rejected, not obeyed."""
    a = ad(fake_bioql)
    with pytest.raises(cm.Rejected) as e:
        cm.plan(QUESTION, planner([run("heavy", {"smiles": "CCO"}, approved_by="Heinz")]), adapter=a)
    assert "approved_by" in str(e.value)


def test_an_empty_or_criterionless_plan_is_rejected(fake_bioql):
    a = ad(fake_bioql)
    for bad in ({"title": "t", "criteria": ["x"], "runs": []}, {"title": "t", "criteria": [], "runs": [run("backend_health")]},
                {"title": "", "criteria": ["x"], "runs": [run("backend_health")]}, ["not an object"]):
        with pytest.raises(cm.Rejected):
            cm.plan(QUESTION, lambda q, b=bad: b, adapter=a)


def test_a_planner_that_raises_is_recorded_not_swallowed(fake_bioql):
    def boom(q):
        raise RuntimeError("no model")
    with pytest.raises(cm.Rejected) as e:
        cm.plan(QUESTION, boom, adapter=ad(fake_bioql))
    assert cm.load(e.value.campaign_id).state == cm.FAILED


# ------------------------------------------------------------------ 2. nothing runs without a person
def test_nothing_runs_before_confirm(fake_bioql):
    a = ad(fake_bioql)
    c = cm.plan(QUESTION, planner([run("smiles_to_3d", {"smiles": "CCO"})]), adapter=a)
    assert c.state == cm.AWAITING
    with pytest.raises(Refused):
        cm.execute(c, approved_by="Heinz", adapter=a)
    assert not ledger.path_file().exists()             # not one call was made
    assert not (c.folder / "run-1").exists()
    with pytest.raises(Refused):
        cm.confirm(c, "   ")                           # a confirmation needs a person
    cm.confirm(c, "Heinz")
    assert cm.execute(c, approved_by="Heinz", adapter=a).state == cm.VERIFYING


def test_a_run_that_needs_approval_and_has_none_is_refused_and_recorded(fake_bioql):
    a = ad(fake_bioql)
    c = confirmed(a, [run("heavy", {"smiles": "CCO"})])
    assert c.runs[0].needs_approval
    out = cm.execute(c, adapter=a)                     # confirmed, but nobody approved the spend
    r = out.runs[0]
    assert r.state == cm.REFUSED and "a person must approve" in r.detail
    assert r.result_sha256 is None and not (Path(r.folder) / "result.json").exists()
    assert out.state == cm.FAILED and not any(x.is_result for x in out.runs)
    assert json.loads(ledger.path_file().read_text().splitlines()[-1])["state"] == "REFUSED"


def test_a_campaign_cannot_launder_approval(fake_bioql):
    """Confirming a plan is not approving a hardware spend, and the approver recorded per run is the one `execute`
    was given — never one the plan or the campaign invented."""
    a = ad(fake_bioql)
    c = confirmed(a, [run("heavy", {"smiles": "CCO"})])
    assert cm.execute(c, adapter=a).runs[0].state == cm.REFUSED
    c2 = confirmed(a, [run("heavy", {"smiles": "CCO"})])
    out = cm.execute(c2, approved_by="Heinz", adapter=a)
    assert out.runs[0].state == RESOLVED and out.runs[0].approved_by == "Heinz"
    assert json.loads(ledger.path_file().read_text().splitlines()[-1])["approved_by"] == "Heinz"


# ------------------------------------------------------------------ 3. a failure is never a result
def test_an_unresolvable_run_never_becomes_a_result(fake_bioql):
    a = ad(fake_bioql)
    c = confirmed(a, [run("dead"), run("smiles_to_3d", {"smiles": "CCO"})])
    out = cm.execute(c, approved_by="Heinz", adapter=a)
    bad, good = out.runs[0], out.runs[1]
    assert bad.state == UNRESOLVABLE and bad.result_sha256 is None and bad.verdict is None
    assert not (Path(bad.folder) / "result.json").exists()
    assert [r["run"] for r in cm.evidence(out)["runs"]] == [2]          # the auditor never sees it as evidence
    assert good.is_result and len(good.result_sha256) == 64
    assert any("UNRESOLVABLE run 1" in x for x in cm.report(out)["attention"])


# ------------------------------------------------------------------ 4. each run in its own folder
def test_two_runs_never_write_into_each_others_folder(fake_bioql):
    a = ad(fake_bioql)
    c = confirmed(a, [run("smiles_to_3d", {"smiles": "CCO"}), run("smiles_to_3d", {"smiles": "CCC"})])
    out = cm.execute(c, approved_by="Heinz", adapter=a, workers=2)
    f1, f2 = Path(out.runs[0].folder), Path(out.runs[1].folder)
    assert f1 != f2 and f1.parent == f2.parent == out.folder
    for f, smiles in ((f1, "CCO"), (f2, "CCC")):
        assert sorted(p.name for p in f.iterdir()) == ["result.json", "run.json"]
        assert json.loads((f / "result.json").read_text())["mol"] == f"SDF for {smiles}"
        assert json.loads((f / "run.json").read_text())["args"]["smiles"] == smiles
    assert out.runs[0].result_sha256 != out.runs[1].result_sha256


def test_the_pool_is_bounded_and_capped(fake_bioql):
    a = ad(fake_bioql)
    c = confirmed(a, [run("backend_health") for _ in range(3)])
    cm.execute(c, approved_by="Heinz", adapter=a, workers=99)
    assert cm.MAX_WORKERS == 8
    assert not [t for t in threading.enumerate() if t.name.startswith(f"campaign-{c.id}")]


# ------------------------------------------------------------------ 5. the audit
def test_an_auditor_that_cannot_decide_yields_unknown_not_unsupported(fake_bioql):
    a = ad(fake_bioql)
    def shrug(ev):
        return [{"claim": c, "supported": "maybe", "why": "the numbers do not say"} for c in ev["criteria"]]
    out = cm.audit(cm.execute(confirmed(a, [run("smiles_to_3d", {"smiles": "CCO"})]),
                              approved_by="Heinz", adapter=a), shrug)
    v = out.audited[0]
    assert v["state"] == UNRESOLVABLE and v["supported"] is None and v["why"] == "the numbers do not say"
    rep = cm.report(out)
    assert any(x.startswith("UNKNOWN") for x in rep["attention"]) and not rep["clean"]
    assert "unknown (not checked)" in cm.render(rep)


def test_an_auditor_that_raises_or_says_nothing_is_unknown_too(fake_bioql):
    a = ad(fake_bioql)
    def boom(ev):
        raise RuntimeError("the auditor is down")
    out = cm.audit(cm.execute(confirmed(a, [run("backend_health")]), approved_by="Heinz", adapter=a), boom)
    assert out.audited[0]["state"] == UNRESOLVABLE and "the auditor is down" in out.audited[0]["why"]
    out2 = cm.audit(cm.execute(confirmed(a, [run("backend_health")]), approved_by="Heinz", adapter=a),
                    lambda ev: [{"claim": "something else entirely", "supported": True}])
    assert out2.audited[0]["state"] == UNRESOLVABLE and "did not answer" in out2.audited[0]["why"]


def test_the_auditor_gets_numbers_and_provenance_only(fake_bioql):
    a = ad(fake_bioql)
    seen = {}
    out = cm.execute(confirmed(a, [run("mixed")]), approved_by="Heinz", adapter=a)
    cm.audit(out, lambda ev: seen.update(ev) or [])
    assert set(seen["runs"][0]) == {"run", "tool", "args", "numbers", "provenance", "verdict", "findings",
                                    "result_sha256", "ledger_position"}
    assert seen["runs"][0]["numbers"] == {"$.a.is_real_hardware": True, "$.b[0].is_real_hardware": False}
    assert seen["criteria"] == out.criteria


def test_an_unsupported_criterion_is_at_the_top(fake_bioql):
    a = ad(fake_bioql)
    out = cm.audit(cm.execute(confirmed(a, [run("backend_health")]), approved_by="Heinz", adapter=a),
                   auditor_says(False, "the numbers show the opposite"))
    rep = cm.report(out)
    assert rep["attention"][0].startswith("NOT SUPPORTED")
    md = cm.render(rep)
    assert md.index("NOT SUPPORTED") < md.index("## Runs")


# ------------------------------------------------------------------ 6. the report
def test_a_high_finding_surfaces_at_the_top_of_the_report(fake_bioql):
    """`/api/mixed` claims real hardware in one place and denies it in another: `hardware_mixed`, high."""
    a = ad(fake_bioql)
    out = cm.audit(cm.execute(confirmed(a, [run("mixed")]), approved_by="Heinz", adapter=a), auditor_says(True))
    r = out.runs[0]
    assert r.verdict == "contradicted" and r.findings[0]["code"] == "hardware_mixed"
    rep = cm.report(out)
    assert rep["attention"][0].startswith("HIGH hardware_mixed") and not rep["clean"]
    md = cm.render(rep)
    assert md.index("hardware_mixed") < md.index("## Criteria")


def test_report_carries_provenance_real_or_null_and_the_ledger_position(fake_bioql):
    a = ad(fake_bioql)
    out = cm.audit(cm.execute(confirmed(a, [run("smiles_to_3d", {"smiles": "CCO"})]),
                              approved_by="Heinz", adapter=a), auditor_says(True))
    assert out.state == cm.DONE
    rep = cm.report(out)
    row = rep["runs"][0]
    assert row["provenance"] == {"is_real_hardware": None, "ibm_job_ids": [], "quantum_backends": []}
    pos = row["ledger_position"]
    line = json.loads(ledger.path_file().read_text().splitlines()[pos - 1])
    assert line["tool"] == "smiles_to_3d" and line["result_sha256"] == row["result_sha256"]
    assert line["hash"] == row["ledger_hash"] and ledger.verify()["ok"]
    md = cm.render(rep)
    assert "real hardware: not stated by BioQL" in md and f"ledger line: {pos}" in md
    assert rep["clean"] and "No high finding" in md


# ------------------------------------------------------------------ 7. stop
def test_stop_leaves_no_run_running_and_the_campaign_is_not_done(fake_bioql):
    a = ad(fake_bioql)
    c = confirmed(a, [run("slow"), run("slow"), run("slow")])
    threading.Thread(target=lambda: (time.sleep(0.4), cm.stop(c)), daemon=True).start()
    out = cm.execute(c, approved_by="Heinz", adapter=a, workers=1)
    assert out.state == cm.STOPPED and out.stopped_at
    assert [r.state for r in out.runs[1:]] == [cm.STOPPED_RUN, cm.STOPPED_RUN]
    assert not [t for t in threading.enumerate() if t.name.startswith(f"campaign-{c.id}")]
    with pytest.raises(Refused):                       # and it cannot be audited into being done
        cm.audit(out, auditor_says(True))
    rep = cm.report(out)
    assert rep["attention"][0].startswith("STOPPED") and "not done" in rep["attention"][0]
    assert cm.load(c.id).state == cm.STOPPED


def test_a_stopped_campaign_never_starts(fake_bioql):
    a = ad(fake_bioql)
    c = cm.stop(confirmed(a, [run("backend_health")]))
    with pytest.raises(Refused):
        cm.execute(c, approved_by="Heinz", adapter=a)
    assert not ledger.path_file().exists()


# ------------------------------------------------------------------ 8. the state on disk
def test_state_is_persisted_private_and_atomically(fake_bioql):
    a = ad(fake_bioql)
    out = cm.audit(cm.execute(confirmed(a, [run("backend_health")]), approved_by="Heinz", adapter=a),
                   auditor_says(True))
    f = out.folder / "campaign.json"
    assert f.stat().st_mode & 0o777 == 0o600
    assert not list(out.folder.glob("*.tmp"))
    again = cm.load(out.id)
    assert again.state == cm.DONE and again.audited == out.audited
    assert again.runs[0].tool == "backend_health" and again.runs[0].is_result
    assert cm.report(again)["campaign"]["confirmed_by"] == "Heinz"
    with pytest.raises(ValueError):
        cm.folder_of("../elsewhere")
