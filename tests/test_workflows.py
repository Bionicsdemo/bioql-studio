"""The catalogue, the search and the workflows, proved mostly through what they refuse to do.

Nothing here reads the owner's own allowlist file: the `studio_home` fixture points BIOQL_STUDIO_HOME at a tmp
directory, so `al.load()` is DEFAULTS alone and every other entry is constructed here. Nothing here calls BioQL
either — `plan()` and `cost()` make no HTTP request at all.
"""
import pytest

from studio import allowlist as al
from studio import campaign as cm
from studio import workflows as wf
from studio.adapter import Adapter


def entries(**extra):
    """DEFAULTS in their real shape, plus the entries a workflow needs, built the way the allowlist builds them."""
    base = {k: al._norm(k, v) for k, v in al.DEFAULTS.items()}
    base["admet_standalone"] = al._norm("admet_standalone", {
        "method": "POST", "path": "/api/pipeline/admet_standalone", "via": "submit", "kind": "compute",
        "required": ["smiles"], "optional": ["target", "indication"], "why": "92 ADMET parameters for one molecule",
        "from_registry": {"name": "admet_standalone", "cost_tier": "medium", "version": 1}})
    base["target_quantum_design"] = al._norm("target_quantum_design", {
        "method": "POST", "path": "/api/quantum/target_design", "via": "submit", "kind": "compute",
        "required": ["target"], "optional": ["pdb_id", "real_qpu", "dock"],
        "from_registry": {"name": "target_quantum_design", "cost_tier": "high"}})
    base["quantum_docking_boltz2"] = al._norm("quantum_docking_boltz2", {
        "method": "POST", "path": "/api/quantum/docking_boltz2", "via": "submit", "kind": "compute",
        "required": ["target_structure_or_sequence", "ligand_smiles"], "optional": ["pocket"],
        "from_registry": {"name": "quantum_docking_boltz2", "cost_tier": "high"}})
    for name, path in (("screening_chembl", "/api/screening/chembl"), ("docking_classical", "/api/docking/classical"),
                       ("sar_mmp", "/api/sar/mmp"), ("reports_generate_dossier", "/api/reports/generate_dossier")):
        base[name] = al._norm(name, {"method": "POST", "path": path, "via": "submit", "kind": "compute",
                                     "why": f"[{'screening' if 'sar' in path or 'screen' in path or 'dock' in path else 'notebook'}] used by BioQL's own window",
                                     "from_registry": {"route_from": "bioql-ui", "route_confirmed_by_bioql": True,
                                                       "area": "screening" if "report" not in path else "notebook"}})
    base["tools_registry"] = al._norm("tools_registry", {
        "method": "GET", "path": "/api/tools/registry", "kind": "status", "why": "[tools] used by BioQL's own window",
        "from_registry": {"route_from": "bioql-ui", "route_confirmed_by_bioql": True, "area": "tools"}})
    base["validation_metrics"] = al._norm("validation_metrics", {
        "method": "GET", "path": "/api/validation/metrics", "kind": "status",
        "why": "[tools] used by BioQL's own window",
        "from_registry": {"route_from": "bioql-ui", "route_confirmed_by_bioql": True, "area": "tools"}})
    base["due_diligence_report"] = al._norm("due_diligence_report", {
        "method": "POST", "path": "/api/pipeline/due_diligence_report", "via": "submit", "kind": "compute",
        "required": ["target_or_product", "jurisdiction"], "optional": ["smiles", "indication"],
        "from_registry": {"name": "due_diligence_report", "cost_tier": "high"}})
    for k, v in extra.items():
        base[k] = al._norm(k, v)
    return base


def ad(**extra):
    return Adapter("http://127.0.0.1:1", entries(**extra), log=False)


# ------------------------------------------------------------------ 1. the catalogue loses nothing
def test_every_enabled_entry_appears_exactly_once():
    ents = entries(off={"method": "GET", "path": "/api/x", "kind": "status", "enabled": False})
    c = wf.catalogue(ents)
    seen = [e["name"] for e in wf.cards(c)]
    assert sorted(seen) == sorted(n for n, e in ents.items() if e["enabled"])
    assert len(seen) == len(set(seen)) == c["total"]
    assert sum(g["count"] for g in c["areas"]) == c["total"]
    assert sum(x["count"] for g in c["areas"] for x in g["groups"]) == c["total"]
    assert [x["name"] for x in c["excluded"]] == ["off"]
    assert "not enabled" in c["excluded"][0]["reason"]


def test_a_malformed_but_enabled_entry_is_kept_and_flagged():
    """A route Studio will never call still has to be visible: dropping it would hide what was asked for."""
    ents = entries(shell={"method": "POST", "path": "/api/exec/run", "kind": "local"})
    c = wf.catalogue(ents)
    found = [e for e in wf.cards(c) if e["name"] == "shell"]
    assert len(found) == 1 and found[0]["callable"] is False and found[0]["problems"]
    assert c["not_callable"] == 1


def test_groups_are_the_areas_a_person_works_in():
    c = wf.catalogue(entries())
    assert set(c["counts"]) <= set(wf.AREA_ORDER)
    assert c["counts"]["screening"] == 3                     # chembl + classical docking + mmp
    assert c["counts"]["tools"] == 2
    # jobs, chem and health come from DEFAULTS; grouping follows the path, not the URL order
    by_name = {e["name"]: e for e in wf.cards(c)}
    assert by_name["pipeline_job"]["area"] == "jobs"
    assert by_name["render_2d"]["area"] == "chem"
    assert by_name["backend_health"]["area"] == "health"
    assert by_name["admet_standalone"]["area"] == "pipeline"


def test_a_card_carries_parameters_person_provenance_and_one_line():
    c = wf.catalogue(entries())
    by_name = {e["name"]: e for e in wf.cards(c)}
    admet = by_name["admet_standalone"]
    assert admet["required"] == ["smiles"] and admet["optional"] == ["target", "indication"]
    assert admet["needs_person"] is True and admet["class"] == wf.PERSON
    assert admet["could_reach_real_hardware"] == wf.HW_PERSON
    assert admet["registry"]["cost_tier"] == "medium"
    assert admet["purpose"] and "\n" not in admet["purpose"]
    # a route BioQL itself answered for says so; one nobody asked about is None, never False
    assert by_name["tools_registry"]["route_confirmed_by_bioql"] is True
    assert by_name["admet_standalone"]["route_confirmed_by_bioql"] is None
    # the area of a discovered route is recorded; the rest are Studio's own classification and say so
    assert by_name["tools_registry"]["area_source"] == "recorded at discovery"
    assert by_name["admet_standalone"]["area_source"] == "classified from the path by Studio"
    assert by_name["backend_health"]["purpose"].startswith("Whether BioQL's backend answers")


def test_no_purpose_recorded_says_so_rather_than_inventing_one():
    c = wf.catalogue(entries(bare={"method": "GET", "path": "/api/bare", "kind": "status"}))
    bare = [e for e in wf.cards(c) if e["name"] == "bare"][0]
    assert bare["purpose"] == "no purpose recorded in the allowlist (GET /api/bare)"
    assert bare["purpose_recorded"] is False


def test_an_entry_with_no_recorded_parameters_is_unknown_not_empty():
    c = wf.catalogue(entries())
    by_name = {e["name"]: e for e in wf.cards(c)}
    assert by_name["screening_chembl"]["parameters_recorded"] is False
    assert by_name["admet_standalone"]["parameters_recorded"] is True
    assert c["parameters_not_recorded"] >= 1


def test_a_read_is_a_read_and_a_compute_asks_a_person():
    c = wf.catalogue(entries())
    by_name = {e["name"]: e for e in wf.cards(c)}
    assert by_name["backend_health"]["class"] == wf.READ
    assert by_name["backend_health"]["could_reach_real_hardware"] == wf.HW_NO
    assert by_name["render_2d"]["class"] == wf.LOCAL
    assert by_name["screening_chembl"]["class"] == wf.PERSON


def test_catalogue_of_the_real_defaults_without_the_owners_file():
    c = wf.catalogue()                                        # BIOQL_STUDIO_HOME is the tmp home: DEFAULTS only
    assert c["total"] == len(al.DEFAULTS) and c["excluded"] == []


# ------------------------------------------------------------------ 2. search matches tokens, never substrings
def test_search_does_not_match_a_substring():
    """`"kd" in "score_breakdown"` is the bug: it once read 60% as 94%."""
    ents = entries(kd_thing={"method": "GET", "path": "/api/x/kd", "kind": "status", "optional": ["kd"],
                             "why": "the kd of a complex"},
                   breakdown={"method": "GET", "path": "/api/x/score_breakdown", "kind": "status",
                              "optional": ["score_breakdown"], "why": "a score breakdown"})
    names = [r["name"] for r in wf.search("kd", ents)]
    assert "kd_thing" in names and "breakdown" not in names
    assert [r["name"] for r in wf.search("breakdown", ents)] == ["breakdown"]


def test_search_ranks_the_exact_name_first():
    ents = entries()
    rows = wf.search("admet_standalone", ents)
    assert rows[0]["name"] == "admet_standalone"
    rows = wf.search("render_2d", ents)
    assert rows[0]["name"] == "render_2d"


def test_search_finds_by_area_parameter_and_purpose():
    ents = entries()
    assert {"screening_chembl", "docking_classical", "sar_mmp"} <= {r["name"] for r in wf.search("screening", ents)}
    smiles = {r["name"] for r in wf.search("smiles", ents)}
    assert {"admet_standalone", "smiles_to_3d"} <= smiles
    jur = wf.search("jurisdiction", ents)
    assert [r["name"] for r in jur] == ["due_diligence_report"] and "parameter" in jur[0]["matched_on"]
    assert "purpose" in wf.search("depiction", ents)[0]["matched_on"]


def test_search_reports_nothing_rather_than_everything():
    assert wf.search("", entries()) == []
    assert wf.search("gravitational", entries()) == []


def test_search_skips_a_disabled_entry():
    ents = entries(off={"method": "GET", "path": "/api/motifs/off", "kind": "status", "enabled": False,
                        "why": "disabled"})
    assert "off" not in {r["name"] for r in wf.search("off", ents)}


# ------------------------------------------------------------------ 3. the workflows are checked against the list
def test_the_shipped_workflows_are_all_plannable_on_this_list():
    rows = {r["name"]: r for r in wf.workflows(entries())}
    assert set(rows) == set(wf.WORKFLOWS)
    for name, r in rows.items():
        assert r["usable"] is True and r["missing"] == [], f"{name}: {r['skipped_because']}"
        assert r["criteria"] and len(set(r["criteria"])) == len(r["criteria"])


def test_a_workflow_naming_a_tool_that_is_not_in_the_list_is_reported_not_dropped(monkeypatch):
    monkeypatch.setitem(wf.WORKFLOWS, "invented", {
        "title": "T", "question": "q?", "criteria": ["c"], "seed": [],
        "steps": [{"tool": "quantum_on_a_whim", "args_from": {}, "why": "because"}]})
    rows = {r["name"]: r for r in wf.workflows(entries())}
    assert "invented" in rows                                  # present, with the reason: not silently gone
    assert rows["invented"]["usable"] is False
    assert rows["invented"]["missing"] == [{"step": 1, "tool": "quantum_on_a_whim",
                                            "reason": "not in Studio's allowlist"}]
    assert "not in Studio's allowlist" in rows["invented"]["skipped_because"]
    with pytest.raises(wf.PlanRejected) as e:
        wf.plan("invented", {}, ad())
    assert "quantum_on_a_whim" in str(e.value) and "allowlist" in str(e.value)


def test_a_disabled_or_malformed_step_is_reported_with_its_reason():
    ents = entries(sar_mmp={"method": "POST", "path": "/api/sar/mmp", "kind": "compute", "enabled": False})
    row = [r for r in wf.workflows(ents) if r["name"] == "screen a library"][0]
    assert row["usable"] is False and row["missing"][0]["reason"] == "in the allowlist but not enabled"


# ------------------------------------------------------------------ 4. plan: the exact shape, nothing trimmed
def test_plan_has_exactly_the_shape_campaign_validates():
    p = wf.plan("molecule at a glance", {"smiles": "CCO"}, ad())
    assert set(p) == {"title", "criteria", "runs"}
    assert all(set(r) == {"tool", "args", "why"} for r in p["runs"])
    assert [r["tool"] for r in p["runs"]] == ["render_2d", "smiles_to_3d", "admet_standalone"]
    assert all(r["args"]["smiles"] == "CCO" for r in p["runs"])


def test_campaign_itself_accepts_the_plan(fake_bioql):
    """The contract proved against the validator it has to satisfy, not against this module's own opinion of it."""
    a = Adapter(fake_bioql, entries(), poll_every=0.05)
    p = wf.plan("molecule at a glance", {"smiles": "CCO"}, a)
    c = cm.plan("Is ethanol worth looking at?", lambda q: p, adapter=a)
    assert c.state == cm.AWAITING and [r.tool for r in c.runs] == [r["tool"] for r in p["runs"]]
    assert [r.needs_approval for r in c.runs] == [False, False, True]


def test_an_unresolved_reference_is_marked_not_guessed():
    p = wf.plan("target to candidate", {"target": "GLP-1R", "target_structure_or_sequence": "4ZUD"}, ad())
    dock = p["runs"][1]
    assert dock["args"]["ligand_smiles"] == "$1.value.result.smiles"      # the reference, not a molecule
    assert "unresolved" in dock["why"] and "$1.value.result.smiles" in dock["why"]
    refs = wf.unresolved(p)
    assert [(r["step"], r["argument"], r["from_step"]) for r in refs] == [
        (2, "ligand_smiles", 1), (3, "smiles", 1)]
    assert all(r["verified"] is False for r in refs)
    # and the seed value is a real value, never a reference
    assert p["runs"][1]["args"]["target_structure_or_sequence"] == "4ZUD"


def test_a_step_with_no_recorded_parameters_says_so_in_the_plan():
    p = wf.plan("screen a library", {}, ad())
    assert all(r["args"] == {} for r in p["runs"])
    assert all(wf.NO_PARAMS in r["why"] for r in p["runs"])


def test_a_missing_seed_value_is_refused_rather_than_invented():
    with pytest.raises(wf.PlanRejected) as e:
        wf.plan("molecule at a glance", {}, ad())
    assert "['smiles']" in str(e.value) and "never guessed" in str(e.value)
    with pytest.raises(wf.PlanRejected):
        wf.plan("molecule at a glance", {"smiles": ""}, ad())


def test_a_seed_key_no_step_uses_is_refused():
    with pytest.raises(wf.PlanRejected) as e:
        wf.plan("molecule at a glance", {"smiles": "CCO", "real_qpu": True}, ad())
    assert "does not use ['real_qpu']" in str(e.value)


def test_a_smuggled_argument_rejects_the_whole_plan(monkeypatch):
    monkeypatch.setitem(wf.WORKFLOWS, "smuggler", {
        "title": "T", "question": "q?", "criteria": ["c"], "seed": ["smiles"],
        "steps": [{"tool": "render_2d", "args_from": {"smiles": "$seed.smiles"},
                   "args": {"real_qpu": True}, "why": "slip one in"}]})
    with pytest.raises(wf.PlanRejected) as e:
        wf.plan("smuggler", {"smiles": "CCO"}, ad())
    assert "does not take ['real_qpu']" in str(e.value) and "step 1" in str(e.value)


def test_an_entry_that_lost_its_parameter_schema_rejects_the_plan_instead_of_dropping_the_argument():
    """An entry that DECLARES it takes nothing rejects the plan naming it — the argument is not quietly dropped.

    The distinction matters and it is `schema_known`. An entry discovered from BioQL's source declares no parameter
    list, and reading that emptiness as "takes nothing" refused every real argument to 414 of the 502 entries: they
    stayed listed and stopped working, which from the outside looks exactly like a missing tool. A declared empty
    schema still refuses; an undeclared one lets the argument through and says the schema is not recorded."""
    ents = entries(render_2d={"method": "POST", "path": "/api/render/2d", "kind": "local",
                              "schema_known": True, "required": [], "optional": []})
    with pytest.raises(wf.PlanRejected) as e:
        wf.plan("molecule at a glance", {"smiles": "CCO"}, Adapter("http://127.0.0.1:1", ents, log=False))
    assert "render_2d" in str(e.value) and "does not take ['smiles']" in str(e.value)


def test_a_forward_or_self_reference_is_refused(monkeypatch):
    for ref in ("$2.value.smiles", "$3.value.smiles", "not-a-reference"):
        monkeypatch.setitem(wf.WORKFLOWS, "bad_ref", {
            "title": "T", "question": "q?", "criteria": ["c"], "seed": [],
            "steps": [{"tool": "admet_standalone", "args_from": {"smiles": ref}, "why": "w"},
                      {"tool": "admet_standalone", "args_from": {"smiles": "$1.value.smiles"}, "why": "w"}]})
        with pytest.raises(wf.PlanRejected) as e:
            wf.plan("bad_ref", {}, ad())
        assert "step 1" in str(e.value)


def test_an_unknown_workflow_names_the_ones_there_are():
    with pytest.raises(wf.PlanRejected) as e:
        wf.plan("cure everything", {}, ad())
    assert "molecule at a glance" in str(e.value)


def test_plan_makes_no_call_even_for_a_compute_step():
    """The adapter here points at a closed port: a plan that touched BioQL would raise, not pass."""
    p = wf.plan("due diligence pack", {"target_or_product": "GLP-1R", "jurisdiction": "US"}, ad())
    assert [r["tool"] for r in p["runs"]] == ["tools_registry", "validation_metrics", "due_diligence_report",
                                              "reports_generate_dossier"]
    assert p["runs"][2]["args"] == {"target_or_product": "GLP-1R", "jurisdiction": "US"}


# ------------------------------------------------------------------ 5. cost, in words
def test_cost_says_what_each_step_is_and_never_a_number():
    ents = entries()
    p = wf.plan("due diligence pack", {"target_or_product": "GLP-1R", "jurisdiction": "US"}, ad())
    c = wf.cost(p, ents)
    assert [s["class"] for s in c["steps"]] == [wf.READ, wf.READ, wf.PERSON, wf.PERSON]
    assert c["reads"] == 2 and c["needs_person"] == 2
    assert c["money"] == c["time"] == wf.NOT_PRICED
    assert c["steps"][0]["could_reach_real_hardware"] == wf.HW_NO
    assert c["steps"][2]["could_reach_real_hardware"] == wf.HW_PERSON
    assert c["steps"][2]["registry_cost_tier"] == "high"      # BioQL's own word, quoted
    assert c["no_unresolved_references"] is True
    assert c["order_enforced"] is False and "neither orders" in c["order_note"]
    for s in c["steps"]:                                       # nothing numeric about price or duration
        assert set(s) & {"seconds", "minutes", "timeout", "usd", "dollars", "credits"} == set()


def test_cost_marks_the_steps_that_cannot_run_yet():
    p = wf.plan("target to candidate", {"target": "GLP-1R", "target_structure_or_sequence": "4ZUD"}, ad())
    c = wf.cost(p, entries())
    assert c["unresolved_total"] == 2 and c["no_unresolved_references"] is False
    assert [s["blocked_until_resolved"] for s in c["steps"]] == [False, True, True]
    assert c["steps"][1]["unresolved"][0]["reference"] == "$1.value.result.smiles"


def test_cost_says_a_person_decides_about_hardware_when_the_arguments_ask_for_it():
    p = {"title": "T", "criteria": ["c"],
         "runs": [{"tool": "target_quantum_design", "args": {"target": "GLP-1R", "real_qpu": True}, "why": "w"}]}
    c = wf.cost(p, entries())
    assert c["steps"][0]["could_reach_real_hardware"] == wf.HW_ASKED
    assert c["steps"][0]["needs_person"] is True
    assert "a person decides" in c["hardware"]


def test_cost_declares_a_declared_hardware_entry_as_such():
    ents = entries(qpu_run={"method": "POST", "path": "/api/quantum/qpu_run", "kind": "hardware"})
    c = wf.cost({"title": "T", "criteria": ["c"], "runs": [{"tool": "qpu_run", "args": {}, "why": "w"}]}, ents)
    assert c["steps"][0]["could_reach_real_hardware"] == wf.HW_DECLARED and c["steps"][0]["needs_person"] is True


def test_cost_of_a_tool_that_is_not_in_the_list_says_unknown_not_cheap():
    c = wf.cost({"title": "T", "criteria": ["c"], "runs": [{"tool": "ghost", "args": {}, "why": "w"}]}, entries())
    assert c["steps"][0]["class"] == wf.UNKNOWN_TOOL and c["steps"][0]["needs_person"] is True
    assert c["unknown_tools"] == 1
    assert "unknown" in c["steps"][0]["could_reach_real_hardware"]


def test_an_entry_whose_parameter_schema_cannot_take_the_step_is_reported_too():
    """`workflows()` must not offer what `plan()` will reject: the owner's `render_2d` is in the list and well-formed,
    and still cannot take `smiles`."""
    ents = entries(render_2d={"method": "POST", "path": "/api/render/2d", "kind": "local",
                              "required": [], "optional": []})
    row = [r for r in wf.workflows(ents) if r["name"] == "molecule at a glance"][0]
    assert row["missing"] == [] and row["usable"] is False
    assert row["argument_problems"] == [{"step": 1, "tool": "render_2d",
                                         "reason": "does not take ['smiles'] (it takes nothing)"}]


# ------------------------------------------------------------------ 6. what the allowlist does not record
def test_discoverys_boilerplate_is_not_a_purpose_and_is_not_searchable():
    """128 of the 193 live entries carry exactly one sentence as their `why`. Reading it as a purpose would make a
    third of the catalogue answer to the word "window"."""
    ents = entries()
    c = wf.catalogue(ents)
    chembl = [e for e in wf.cards(c) if e["name"] == "screening_chembl"][0]
    assert chembl["purpose_recorded"] is False and wf.BOILERPLATE in chembl["purpose"]
    assert c["purpose_not_recorded"] >= 5
    assert wf.search("window", ents) == []


def test_one_route_under_two_names_is_said_to_be_one_route():
    ents = entries(pipeline_admet_standalone={"method": "POST", "path": "/api/pipeline/admet_standalone",
                                              "via": "submit", "kind": "compute"})
    c = wf.catalogue(ents)
    by_name = {e["name"]: e for e in wf.cards(c)}
    assert by_name["admet_standalone"]["same_route_as"] == ["pipeline_admet_standalone"]
    assert by_name["pipeline_admet_standalone"]["same_route_as"] == ["admet_standalone"]
    assert c["duplicate_routes"]["POST /api/pipeline/admet_standalone"] == ["admet_standalone",
                                                                            "pipeline_admet_standalone"]
    # the duplicate is the one with no schema: both are listed, and the difference is visible
    assert by_name["pipeline_admet_standalone"]["parameters_recorded"] is False


def test_the_second_level_is_labelled_as_studios_own_reading():
    c = wf.catalogue(entries())
    jobs = [a for a in c["areas"] if a["area"] == "jobs"][0]
    assert "not stated by BioQL" in jobs["group_source"]
    assert wf.family_of("/api/quantum/peptide_design") == "quantum/peptide"
    assert wf.family_of("/api/screening/chembl") == "screening"
    assert wf.family_of("/health") == "health"
    assert set(c["group_counts"]["screening"]) == {"screening", "docking", "sar"}


def test_search_returns_everything_unless_a_limit_is_asked_for():
    ents = {f"thing_{i}": al._norm(f"thing_{i}", {"method": "GET", "path": f"/api/motifs/{i}", "kind": "status"})
            for i in range(60)}
    assert len(wf.search("thing", ents)) == 60
    assert len(wf.search("thing", ents, limit=5)) == 5


def test_a_seed_value_that_looks_like_a_reference_is_refused():
    with pytest.raises(wf.PlanRejected) as e:
        wf.plan("molecule at a glance", {"smiles": "$1.value.result.smiles"}, ad())
    assert "looks like a step reference" in str(e.value)


def test_a_read_is_not_said_to_be_missing_a_body():
    """Two of the four steps of the diligence pack are GET reads: a GET takes no body, and saying the allowlist lost
    one would be a false finding."""
    p = wf.plan("due diligence pack", {"target_or_product": "GLP-1R", "jurisdiction": "US"}, ad())
    assert wf.NO_PARAMS not in p["runs"][0]["why"] and wf.NO_PARAMS not in p["runs"][1]["why"]
    assert wf.NO_PARAMS in p["runs"][3]["why"]               # the dossier export is a compute route with no schema
    c = wf.cost(p, entries())
    assert [s["sent_without_a_body"] for s in c["steps"]] == [False, False, False, True]


def test_one_route_under_two_names_that_disagree_about_approval_is_flagged():
    """`chem_amp_bms` (local, no approval) and `amp_bms_md` (compute, approval) are one POST on the real list: the
    gate then depends on which name is used."""
    ents = entries(chem_amp_bms={"method": "POST", "path": "/api/chem/amp_bms", "kind": "local"},
                   amp_bms_md={"method": "POST", "path": "/api/chem/amp_bms", "via": "submit", "kind": "compute"})
    c = wf.catalogue(ents)
    by_name = {e["name"]: e for e in wf.cards(c)}
    assert by_name["chem_amp_bms"]["route_rules_disagree"] is True
    assert by_name["amp_bms_md"]["route_rules_disagree"] is True
    assert c["routes_whose_rules_disagree"]["POST /api/chem/amp_bms"] == ["amp_bms_md", "chem_amp_bms"]
    assert by_name["admet_standalone"]["route_rules_disagree"] is False


def test_a_search_row_does_not_print_a_group_the_catalogue_would_merge():
    assert "group" not in wf.search("admet_standalone", entries())[0]


def test_the_second_level_uses_bioqls_own_route_directory_when_there_is_one():
    assert wf.family_of("/api/quantum/docking/boltz2") == "quantum/docking"
    assert wf.family_of("/api/pipeline/job/{job_id}/result") == "pipeline/job"


def test_a_compute_step_with_no_recorded_parameters_is_flagged_as_sent_with_no_body():
    p = wf.plan("screen a library", {}, ad())
    c = wf.cost(p, entries())
    assert [s["sent_without_a_body"] for s in c["steps"]] == [True, True, True]
    assert c["sent_without_a_body"] == 3
