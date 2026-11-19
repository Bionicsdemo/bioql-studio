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
def job(url, **extra):
    return ad(url, s={"method": "POST", "path": "/api/quantum/thing", "via": "submit", "kind": "local",
                      "optional": ["which"]}, **extra)


def test_a_receipt_is_never_a_result(fake_bioql):
    """BioQL answers a submit with {job_id, status: "accepted", poll_url} and no science in it. The real run of
    2026-11-19 came back as a receipt and an earlier version of this adapter reported it RESOLVED — the exact
    "produce a plausible value instead of stopping" defect the three-state invariant exists to prevent."""
    res = job(fake_bioql).call("s", {"which": "j2"})
    assert res.state == RESOLVED and res.value["status"] == "completed"
    assert res.value.get("result") or res.value.get("job_id") == "j2"
    assert "accepted" not in str(res.value.get("status"))
    assert res.meta["provenance"] == {"is_real_hardware": True, "ibm_job_ids": ["d0abc123"], "quantum_backends": []}


def test_a_receipt_with_no_job_id_is_unresolvable(fake_bioql):
    res = job(fake_bioql).call("s", {"which": "j7"})
    assert res.state == UNRESOLVABLE and "no job id" in res.detail


def test_a_failed_job_is_unresolvable(fake_bioql):
    res = job(fake_bioql).call("s", {"which": "j3"})
    assert res.state == UNRESOLVABLE and "boom" in res.detail


def test_the_result_is_fetched_from_the_jobs_own_route(fake_bioql):
    """A large result is not in the status body: BioQL keeps it at /api/pipeline/job/<id>/result (95 988 bytes for
    one real ADMET run). The route is built from the job id, never from the `result_url` the answer names."""
    res = job(fake_bioql).call("s", {"which": "j5"})
    assert res.state == RESOLVED and res.value["result"] == {"admet": {"logp": 1.2}, "is_real_hardware": False}
    assert res.meta["provenance"]["is_real_hardware"] is False


def test_completed_with_nothing_in_it_is_unresolvable(fake_bioql):
    res = job(fake_bioql).call("s", {"which": "j6"})
    assert res.state == UNRESOLVABLE and "no result" in res.detail


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
    res = job(fake_bioql).call("s", {"which": "j4"})
    assert res.state == UNRESOLVABLE and "unknown state" in res.detail


def test_every_call_lands_in_the_signed_notebook(fake_bioql, studio_home):
    """The notebook existed and nothing used it: the adapter called ledger.append directly, so every real row read
    back UNSIGNED. The lab notebook is the artefact a due-diligence reader is handed; it has to be the default."""
    from studio import notebook
    ad(fake_bioql).call("backend_health")
    r = notebook.verify()
    assert r["lines"] == 1 and r["unsigned"] == 0 and r["ok"] is True
    assert r["current_key_id"]


def test_a_mac_that_cannot_sign_still_records_the_call(fake_bioql, studio_home, monkeypatch):
    """A signing failure must never cost the line. It is reported as unsigned, never passed off as signed."""
    from studio import notebook
    monkeypatch.setattr(notebook, "record", lambda *a, **k: (_ for _ in ()).throw(notebook.NoCrypto("no cryptography")))
    res = ad(fake_bioql).call("backend_health")
    assert res.state == RESOLVED
    import json
    row = json.loads(ledger.path_file().read_text().splitlines()[-1])
    assert row["tool"] == "backend_health" and "NoCrypto" in row["unsigned_because"]
    assert notebook.verify()["unsigned"] == 1


@pytest.mark.parametrize("bad", ["..", ".", "...", "..-.", "../x", "a/b", "j1?x=1", "", "%2e%2e"])
def test_a_path_segment_can_never_change_the_route(fake_bioql, bad):
    """`..` matched the old id pattern, and quote() does not escape a dot even with safe="": job_id=".." built
    /api/pipeline/job/.. , which a server normalises to a DIFFERENT route than the allowlist named. The whole point
    of a closed list is that the caller cannot choose the route."""
    a = ad(fake_bioql)
    with pytest.raises(Refused):
        a.call("pipeline_job", {"job_id": bad})


def test_an_ordinary_job_id_still_works(fake_bioql):
    assert ad(fake_bioql).call("pipeline_job", {"job_id": "j1"}).state == RESOLVED


def test_an_undeclared_schema_lets_a_real_argument_through(fake_bioql):
    """Unknown is not "takes nothing". 414 of the 502 entries come from BioQL's own source, which declares no
    parameter list; refusing their arguments left them listed and unusable — from the outside, a missing tool."""
    a = ad(fake_bioql, found={"method": "POST", "path": "/api/smiles-to-3d", "kind": "local"})
    assert a.entries["found"]["schema_known"] is False
    assert a.call("found", {"smiles": "CCO", "anything": 1}).state == RESOLVED


def test_an_undeclared_schema_still_never_lets_the_caller_choose_the_route(fake_bioql):
    a = ad(fake_bioql, found={"method": "POST", "path": "/api/smiles-to-3d", "kind": "local"})
    for bad in ({"endpoint": "/api/ssh/execute"}, {"url": "http://x"}, {"approved_by": "me"}, {"by": "me"}):
        with pytest.raises(Refused, match="never the caller"):
            a.call("found", bad)


def test_a_declared_schema_is_still_strict(fake_bioql):
    a = ad(fake_bioql)
    with pytest.raises(Refused, match="does not take"):
        a.call("tools_registry", {"smuggled": 1})


def test_a_file_entry_never_erases_a_builtins_schema(studio_home):
    """A discovery tool writes `"required": []` because it only read path parameters. Merging that over a known
    schema turned render_2d(smiles=…) into a route that takes nothing."""
    f = al.path_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"render_2d": {"method": "POST", "path": "/api/render/2d", "kind": "local",
                                           "required": [], "optional": []}}))
    e = al.load()["render_2d"]
    assert e["required"] == ["smiles"] and e["schema_known"] is True


def test_discovery_can_never_overwrite_a_builtin(studio_home):
    """A built-in was written by hand; a discovery tool only guesses from a route's Allow header. One such guess
    turned smiles_to_3d from POST/local into GET/status — and a GET carries no body, so the engine answered
    "No SMILES provided" to a call that carried the SMILES."""
    f = al.path_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"smiles_to_3d": {
        "method": "GET", "path": "/api/smiles-to-3d", "kind": "status", "via": "direct", "required": [],
        "from_registry": {"route_from": "bioql-ui"}}}))
    e = al.load()["smiles_to_3d"]
    assert e["method"] == "POST" and e["kind"] == "local" and e["required"] == ["smiles"]


def test_a_hand_edit_of_a_builtin_is_still_honoured(studio_home):
    """Only a DISCOVERY entry is overruled. The owner editing their own file still wins."""
    f = al.path_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"smiles_to_3d": {"method": "GET", "path": "/api/smiles-to-3d", "kind": "status"}}))
    assert al.load()["smiles_to_3d"]["method"] == "GET"
