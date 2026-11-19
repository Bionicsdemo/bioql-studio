import json

import pytest

from studio import allowlist as al
from studio import ledger
from studio.adapter import Adapter, Refused, provenance, wants_hardware
from studio.resolution import ABSENT, RESOLVED, UNRESOLVABLE, Unresolvable, absent, resolved, unresolvable


def entries(**extra):
    base = {k: al._norm(k, v) for k, v in al.DEFAULTS.items()}
    for k, v in extra.items():
        base[k] = al._norm(k, v)
    return base


def ad(url, **extra):
    return Adapter(url, entries(**extra), poll_every=0.05)


# ------------------------------------------------------------------ the three states
def test_resolution_semantics():
    r, a, u = resolved({"x": 1}, "s"), absent("s", "none"), unresolvable("s", "timeout")
    assert r.value == {"x": 1} and r.or_default(0) == {"x": 1}
    assert a.or_default(7) == 7
    with pytest.raises(Unresolvable):
        u.or_default(7)
    with pytest.raises(Unresolvable):
        u.value
    with pytest.raises(LookupError):
        a.value
    for x in (r, a, u):
        with pytest.raises(TypeError):
            bool(x)
    with pytest.raises(TypeError):
        resolved(None, "s")


def test_resolved_call_and_ledger(fake_bioql):
    res = ad(fake_bioql).call("smiles_to_3d", {"smiles": "CCO"})
    assert res.state == RESOLVED and res.value["mol"] == "SDF for CCO"
    assert res.meta["provenance"] == {"is_real_hardware": None, "ibm_job_ids": [], "quantum_backends": []}
    rows = [json.loads(l) for l in ledger.path_file().read_text().splitlines()]
    assert rows[-1]["tool"] == "smiles_to_3d" and rows[-1]["state"] == RESOLVED and len(rows[-1]["result_sha256"]) == 64
    assert ledger.verify()["ok"]


def test_absent_on_404_only_where_declared(fake_bioql):
    a = ad(fake_bioql)
    assert a.call("pipeline_job", {"job_id": "nope"}).state == ABSENT
    a2 = ad(fake_bioql, missing={"method": "POST", "path": "/api/missing", "kind": "local"})
    assert a2.call("missing").state == UNRESOLVABLE


@pytest.mark.parametrize("path,why", [("/api/boom", "HTTP 500"), ("/api/notjson", "not JSON"),
                                      ("/api/err", "reported an error")])
def test_failures_are_unresolvable_never_absent(fake_bioql, path, why):
    res = ad(fake_bioql, t={"method": "POST", "path": path, "kind": "local"}).call("t")
    assert res.state == UNRESOLVABLE and why in res.detail
    with pytest.raises(Unresolvable):
        res.or_default({})


def test_timeout_and_unreachable_are_unresolvable(fake_bioql):
    res = ad(fake_bioql, t={"method": "POST", "path": "/api/slow", "kind": "local", "timeout": 0.3}).call("t")
    assert res.state == UNRESOLVABLE
    res2 = Adapter("http://127.0.0.1:9", entries()).call("backend_health")
    assert res2.state == UNRESOLVABLE and "could not reach" in res2.detail


# ------------------------------------------------------------------ what is refused
def test_refusals(fake_bioql):
    a = ad(fake_bioql, off={"method": "GET", "path": "/health", "kind": "status", "enabled": False})
    with pytest.raises(Refused):
        a.call("not_listed")
    with pytest.raises(Refused):
        a.call("off")
    with pytest.raises(Refused):
        a.call("smiles_to_3d", {"smiles": "C", "extra": 1})
    with pytest.raises(Refused):
        a.call("smiles_to_3d", {})
    for bad in ("../health", "a/b", "x?y=1", ""):
        with pytest.raises(Refused):
            a.call("pipeline_job", {"job_id": bad})
    rows = [json.loads(l) for l in ledger.path_file().read_text().splitlines()]
    assert all(r["state"] == "REFUSED" for r in rows) and len(rows) == 8


def test_hardware_needs_a_person(fake_bioql):
    a = ad(fake_bioql, q={"method": "POST", "path": "/api/mixed", "kind": "hardware"},
           sim={"method": "POST", "path": "/api/smiles-to-3d", "kind": "local", "required": ["smiles"],
                "optional": ["backend", "use_real_hardware"]})
    with pytest.raises(Refused):
        a.call("q")
    assert a.call("q", approved_by="Heinz").state == RESOLVED
    with pytest.raises(Refused):
        a.call("sim", {"smiles": "C", "backend": "ibm_fez"})
    with pytest.raises(Refused):
        a.call("sim", {"smiles": "C", "use_real_hardware": True})
    assert a.call("sim", {"smiles": "C", "backend": "aer_simulator", "use_real_hardware": False}).state == RESOLVED


def test_wants_hardware_matches_whole_keys():
    assert wants_hardware({"backend": "ibm_marrakesh"})
    assert not wants_hardware({"backend_notes": "ibm_fez"})
    assert not wants_hardware({"hardware_report": True})


def test_deny_list_holds_even_from_the_owners_file(fake_bioql, studio_home):
    for p in ("/api/ssh/execute", "/api/scripts/execute", "/api/scripts/repl", "/api/recovery/cancel_queued", "/api/auth/validate"):
        with pytest.raises(ValueError):
            al.save_entry("x_" + p.split("/")[-1], {"method": "POST", "path": p, "kind": "local"})
    f = al.path_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"sneaky": {"method": "POST", "path": "/api/ssh/execute", "kind": "local"}}))
    a = Adapter(fake_bioql, al.load())
    assert "sneaky" not in [e["name"] for e in a.tools()]
    with pytest.raises(Refused):
        a.call("sneaky")


# ------------------------------------------------------------------ provenance: real-or-null
def test_provenance_is_copied_never_inferred(fake_bioql):
    a = ad(fake_bioql, m={"method": "POST", "path": "/api/mixed", "kind": "local"},
           p={"method": "POST", "path": "/api/plain", "kind": "local"})
    assert a.call("m").meta["provenance"]["is_real_hardware"] == "mixed"
    assert a.call("p").meta["provenance"]["is_real_hardware"] is None
    assert provenance({"is_real_hardware": "yes"})["is_real_hardware"] is None      # only a real boolean counts
    assert provenance({"x": {"ibm_job_ids": ["a", "b"]}})["ibm_job_ids"] == ["a", "b"]


# ------------------------------------------------------------------ jobs
def test_job_is_followed_on_bioqls_own_route(fake_bioql):
    a = ad(fake_bioql, s={"method": "POST", "path": "/api/submit", "kind": "local", "optional": ["which"]})
    res = a.call("s", {"which": "j2"})
    assert res.state == RESOLVED and res.value["status"] == "completed"
    assert res.meta["provenance"] == {"is_real_hardware": True, "ibm_job_ids": ["d0abc123"], "quantum_backends": []}
    failed = a.call("s", {"which": "j3"})
    assert failed.state == UNRESOLVABLE and "boom" in failed.detail


def test_via_submit_uses_bioqls_async_door_with_the_allowlists_own_endpoint(fake_bioql, fake_log):
    """A heavy pipeline goes through POST /api/pipeline/submit {endpoint, params}, as BioQL's own UI does. The
    endpoint sent is the ENTRY's path — a caller cannot name one."""
    FakeBioQL = fake_log
    a = ad(fake_bioql, heavy={"method": "POST", "path": "/api/quantum/peptide_design", "via": "submit",
                              "kind": "compute", "required": ["target"], "optional": ["which"]})
    res = a.call("heavy", {"target": "GLP-1R", "which": "j1"}, approved_by="a person")
    assert res.state == RESOLVED and res.value["status"] == "completed"
    assert FakeBioQL.submits == [{"endpoint": "/api/quantum/peptide_design",
                                  "params": {"target": "GLP-1R", "which": "j1"}}]
    with pytest.raises(Refused):                       # still needs a person, submit door or not
        a.call("heavy", {"target": "GLP-1R"})
    with pytest.raises(Refused):                       # and cannot be told to post somewhere else
        a.call("heavy", {"target": "GLP-1R", "endpoint": "/api/ssh/execute"}, approved_by="a person")


def test_via_must_be_known(fake_bioql, studio_home):
    with pytest.raises(ValueError):
        al.save_entry("odd", {"method": "POST", "path": "/api/x", "via": "sideways", "kind": "local"})
    with pytest.raises(ValueError):
        al.save_entry("getsubmit", {"method": "GET", "path": "/api/x", "via": "submit", "kind": "local"})


def test_ledger_detects_tampering(fake_bioql):
    a = ad(fake_bioql)
    a.call("backend_health")
    a.call("smiles_to_3d", {"smiles": "C"})
    f = ledger.path_file()
    lines = f.read_text().splitlines()
    row = json.loads(lines[0]); row["state"] = "ABSENT"
    f.write_text(json.dumps(row) + "\n" + lines[1] + "\n")
    assert not ledger.verify()["ok"]


# ------------------------------------------------------------------ registry → allowlist
def test_from_registry_uses_bioqls_real_tiers():
    """BioQL's registry only ever says cost_tier high or medium (63/63 pipelines, 2026-11-19): it never states whether
    a pipeline can reach a QPU. So every pipeline added from the registry asks for a person, whatever its tier."""
    hi = {"name": "peptide_design", "kind": "http", "endpoint": "/api/quantum/peptide_design",
          "schema": {"required": ["target"], "cost_tier": "high", "confirmation_required": True}}
    med = {"name": "xtb", "kind": "http", "endpoint": "/api/chem/xtb_single_point",
           "schema": {"required": ["smiles"], "cost_tier": "medium"}}
    for t in (hi, med):
        e = al.from_registry(t)
        assert e["kind"] == "compute" and e["approval"], e
    e = al.from_registry(med, kind="local")                 # only the owner, by hand, can make one cheap
    assert e["kind"] == "local" and not e["approval"]
    with pytest.raises(ValueError):
        al.from_registry({"name": "admet", "kind": "smiles", "endpoint": None})


def test_real_qpu_is_bioqls_own_parameter_name():
    """`real_qpu` and `backend` are the names BioQL's registry actually uses."""
    assert wants_hardware({"real_qpu": True})
    assert not wants_hardware({"real_qpu": False})
    assert wants_hardware({"backend": "ibm_kingston"})


def test_bom_and_nul_in_a_streamed_body_still_parse(fake_bioql):
    a = ad(fake_bioql, b={"method": "POST", "path": "/api/bom", "kind": "local"})
    assert a.call("b").value == {"ok": 1}


def test_unknown_job_state_is_not_a_result(fake_bioql):
    a = ad(fake_bioql, s={"method": "POST", "path": "/api/submit", "kind": "local", "optional": ["which"]})
    res = a.call("s", {"which": "j4"})
    assert res.state == UNRESOLVABLE and "unknown state" in res.detail
