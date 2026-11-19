"""Cards. Every fixture is a shape BioQL really produces — the recorded payloads of 2026-11-19, and the dot-paths
BioQL's own `cardRegistry.ts` gates its cards on.

What is REAL here and what is CONSTRUCTED, said plainly because the difference matters:
  REAL, recorded 2026-11-19 — the ADMET envelope (job 5a362e56-077, paracetamol): `results[0].admet_classical.
      raw_predictions`, `summary`, `quantum_backend: "NONE"`, `quantum_execution: "CLASSICAL_FALLBACK"`; the
      `smiles_to_3d` answer `{mol, smiles}`; the `render_2d` answer `{image_base64, format}`; the motif catalogue
      `{categories, total_motifs}`; the submit receipt `{job_id, status: "accepted", poll_url}`. The first four are
      also the fixtures of `tests/test_verify.py` and `studio/rails.py`, from the same run.
  REAL SHAPE, values constructed — the 12 TDC endpoint NAMES (ames, bbb_martins, cyp1a2_veith, …) are BioQL's; the
      nulls, the `null_reason` strings and the `float("nan")` are written here to exercise rule 1. BioQL's
      honest-value contract (`HonestEngineCards.tsx`) and `bionics_bridge`'s `float("nan")` default are where both
      forms come from, but this exact combination was not read off a recorded run and is not called one.
  CONSTRUCTED — `fda_report`, the docking payload, the `iterations` series and the unknown-tool payload. Their
      shapes follow `cardRegistry.ts` (`structure_3d.pdb`, `docking.binding_affinity_kcal`) and
      `ChatMessage.renderOptimizationConvergence` (`iterations[].iteration/.convergence`); the numbers are made up.
      The read of BioQL's ADMET route in the monolith was denied by permissions, so nothing here claims to be the
      verbatim body of that route beyond the five fields named above.
"""
import json
import math

import pytest

from studio.cards import (ABSENT_CELL, BLOCK_TYPES, KINDS, MAX_FACTS, MAX_SERIES_POINTS, MAX_TABLE_ROWS, card,
                          provenance)

# --------------------------------------------------------------------------- fixtures

# The real ADMET run. 12 endpoints, of which three are missing in the three ways BioQL writes a missing one:
# a bare null, an honest-value wrapper with its reason, and `float("nan")` (bionics_bridge's default).
ADMET = {
    "pipeline": "ADMET_STANDALONE", "quantum_backend": "NONE", "quantum_execution": "CLASSICAL_FALLBACK",
    "ibm_job_ids": [], "molecules_analyzed": 1,
    "summary": {"classical_fallback": 0, "quantum_success": 1, "total_analyzed": 1},
    "results": [{
        "molecule_id": 1, "smiles": "CC(=O)Nc1ccc(O)cc1", "status": "SUCCESS",
        "method": "bionics-admet-lgbm-ensemble",
        "admet_classical": {
            "backend": "Bionics LightGBM (classical ML)",
            "quantum_engine": "classical LightGBM ensemble (no quantum hardware in this prediction)",
            "raw_predictions": {
                "ames": 0.1337, "bbb_martins": 0.8123, "cyp1a2_veith": 0.4410, "cyp2c9_veith": 0.2203,
                "cyp2d6_veith": 0.1105, "cyp3a4_veith": 0.3301, "herg": 0.0712, "dili": 0.2290,
                "caco2_wang": -4.71,
                "ld50_zhu": None,
                "clearance_hepatocyte_az": {"value": None,
                                            "null_reason": "no hepatocyte model for this scaffold"},
                "half_life_obach": float("nan"),
            },
        },
    }],
    "fda_report": {"status": "no record", "source": "openFDA /drug/drugsfda.json",
                   "note": "queried and the source has no approval for this structure"},
}

SMILES_TO_3D = {"mol": "\n     RDKit          3D\n\n 20 20  0  0  0  0  0  0  0  0999 V2000\n"
                       "    1.2000    0.0000    0.0000 C   0  0\nM  END\n",
                "smiles": "CC(=O)Nc1ccc(O)cc1"}

RENDER_2D = {"image_base64": "iVBORw0KGgoAAAANSUhEUg", "format": "png"}

MOTIFS = {"categories": {"alkyls": [{"name": "methyl"}, {"name": "ethyl"}],
                         "aromatics": [{"name": "phenyl"}]},
          "total_motifs": 120}

RECEIPT = {"job_id": "5a362e56-077", "status": "accepted", "poll_url": "/api/pipeline/job/5a362e56-077",
           "message": "Pipeline submitted. Poll for results."}


def blocks_of(spec, kind):
    return [b for b in spec["blocks"] if b["type"] == kind]


def absent_about(spec, needle):
    return [b for b in blocks_of(spec, "absent") if needle in b["what"]]


def leaves(o, path=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from leaves(v, f"{path}.{k}" if path else k)
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from leaves(v, f"{path}[{i}]")
    else:
        yield path, o


ALLOWED = {"facts": {"rows"}, "table": {"columns", "rows"}, "molecule2d": {"png_base64", "smiles"},
           "molecule3d": {"sdf"}, "series": {"x_label", "y_label", "points"}, "text": {"text"},
           "links": {"items"}, "absent": {"what", "why"}}


def check_schema(spec):
    """The contract, checked field by field: exact top-level keys, a known kind, and only the eight block types with
    only their own keys. Run over every fixture and over every junk payload, because a spec the window cannot read
    is the same failure as a wrong number."""
    assert set(spec) == {"kind", "title", "subtitle", "provenance", "verdict", "findings", "blocks"}
    assert spec["kind"] in KINDS
    assert isinstance(spec["title"], str) and spec["title"]
    assert spec["subtitle"] is None or isinstance(spec["subtitle"], str)
    p = spec["provenance"]
    assert set(p) == {"is_real_hardware", "ibm_job_ids", "backends"}
    assert p["is_real_hardware"] in (True, False, None, "mixed")
    assert isinstance(p["ibm_job_ids"], list) and isinstance(p["backends"], list)
    assert spec["verdict"] is None or isinstance(spec["verdict"], str)
    assert isinstance(spec["findings"], list)
    assert spec["blocks"], "a card with no blocks is not a card"
    for b in spec["blocks"]:
        assert b["type"] in BLOCK_TYPES, b
        assert set(b) - {"type"} <= ALLOWED[b["type"]], b
        if b["type"] == "facts":
            assert len(b["rows"]) <= MAX_FACTS
            for r in b["rows"]:
                assert set(r) <= {"label", "value", "unit", "note"}
                assert "label" in r and "value" in r
                # rule 1 from the other side: a fact is never a dressed-up absence
                assert r["value"] not in (None, "", "N/A", "n/a"), r
                assert not (isinstance(r["value"], float) and not math.isfinite(r["value"])), r
        if b["type"] == "table":
            assert len(b["rows"]) <= MAX_TABLE_ROWS
            assert all(len(row) == len(b["columns"]) for row in b["rows"])
        if b["type"] == "series":
            assert len(b["points"]) <= MAX_SERIES_POINTS
            assert all(len(pt) == 2 and all(isinstance(x, (int, float)) for x in pt) for pt in b["points"])
    # a card the window can hand to Claude, and Claude back: NaN is not JSON
    json.dumps(spec, allow_nan=False)


# --------------------------------------------------------------------------- the contract holds everywhere
@pytest.mark.parametrize("payload,tool", [(ADMET, "admet_standalone"), (SMILES_TO_3D, "smiles_to_3d"),
                                          (RENDER_2D, "render_2d"), (MOTIFS, "motifs_list"),
                                          (RECEIPT, "pipeline_submit")])
def test_every_real_payload_makes_a_legal_card(payload, tool):
    check_schema(card(payload, tool=tool))


@pytest.mark.parametrize("junk", [None, {}, [], "", 0, True, "a bare string", [1, 2, 3],
                                  {"a": {"b": {"c": {"d": {"e": 1}}}}}, [[1, 2], [3, 4]],
                                  {"": None}, {"x": float("nan")}])
def test_junk_still_renders_and_still_obeys_the_contract(junk):
    spec = card(junk)
    check_schema(spec)
    assert spec["blocks"], junk


# --------------------------------------------------------------------------- rule 1: absent is drawn
def test_an_absent_endpoint_is_drawn_with_its_reason():
    spec = card(ADMET, tool="admet_standalone")
    assert spec["kind"] == "admet"
    whys = {b["what"]: b["why"] for b in blocks_of(spec, "absent")}
    # BioQL's own reason, verbatim
    assert whys["clearance_hepatocyte_az"] == "no hepatocyte model for this scaffold"
    # a bare null states the literal fact and invents no cause
    assert "no reason given" in whys["ld50_zhu"] and "model" not in whys["ld50_zhu"]
    # bionics_bridge's float("nan") is an absence, not a number
    assert "NaN" in whys["half_life_obach"]
    # and the nine endpoints that DID compute are facts
    rows = {r["label"]: r["value"] for b in blocks_of(spec, "facts") for r in b["rows"]}
    assert rows["ames"] == 0.1337 and rows["bbb_martins"] == 0.8123 and rows["cyp1a2_veith"] == 0.4410


def test_an_absent_field_is_never_a_zero_an_na_or_an_empty_cell():
    spec = card(ADMET)
    for b in blocks_of(spec, "facts"):
        labels = {r["label"] for r in b["rows"]}
        assert "ld50_zhu" not in labels and "half_life_obach" not in labels
    for b in blocks_of(spec, "table"):
        for row in b["rows"]:
            assert None not in row and "N/A" not in row and "" not in row


def test_a_missing_table_cell_says_absent_and_the_column_says_why():
    """A null cell is the empty cell rule 1 forbids. The cell carries a word and the column carries the reason."""
    payload = {"candidates": [{"id": "A", "score": 0.9, "kd_nM": 12.0},
                              {"id": "B", "score": 0.7, "kd_nM": None},
                              {"id": "C", "score": 0.5}]}
    spec = card(payload)
    table = blocks_of(spec, "table")[0]
    kd = table["columns"].index("kd_nM (nM)")
    assert table["rows"][0][kd] == 12.0
    assert table["rows"][1][kd] == ABSENT_CELL and table["rows"][2][kd] == ABSENT_CELL
    why = absent_about(spec, "candidates.kd_nM")[0]["why"]
    assert "rows 1, 2" in why and "no reason given" in why and "does not carry this field" in why
    check_schema(spec)


# --------------------------------------------------------------------------- rule 2: provenance is copied
def test_a_quantum_named_field_over_a_classical_run_is_not_a_provenance_claim():
    spec = card(ADMET, tool="admet_standalone")
    p = spec["provenance"]
    assert p["is_real_hardware"] is None, "no boolean in the payload means null, never False and never True"
    assert p["ibm_job_ids"] == []
    assert "NONE" in p["backends"], "BioQL's own word for no backend, copied verbatim"
    assert "Bionics LightGBM (classical ML)" in p["backends"]


def test_none_is_a_value_and_an_empty_list_is_not_a_null():
    """BioQL's `quantum_backend: "NONE"` is an honest denial a reader is not misled by (studio/claims.py), and
    `ibm_job_ids: []` is the payload answering "none of these". Degrading either into "null, no reason given" throws
    away the two fields that say what actually ran."""
    spec = card(ADMET)
    rows = {r["label"]: r["value"] for b in blocks_of(spec, "facts") for r in b["rows"]}
    assert rows["quantum_backend"] == "NONE"
    assert not absent_about(spec, "quantum_backend")
    assert absent_about(spec, "ibm_job_ids")[0]["why"] == "the payload holds an empty list"
    # and the spellings BioQL's own frontend does render as N/A are still absences
    na = card({"logp": "N/A", "logs": ""})
    assert {b["what"] for b in blocks_of(na, "absent")} == {"logp", "logs"}


def test_a_device_name_alone_is_recorded_as_a_backend_and_not_as_hardware():
    spec = card({"energy_kcal_mol": -12.3, "quantum_backend": "ibm_fez"})
    assert spec["provenance"] == {"is_real_hardware": None, "ibm_job_ids": [], "backends": ["ibm_fez"]}


def test_provenance_is_copied_from_the_payload_including_mixed():
    one = card({"is_real_hardware": True, "ibm_job_id": "dbi185ghoj4c73b5d1p0", "quantum_backend": "ibm_fez"})
    assert one["provenance"] == {"is_real_hardware": True, "ibm_job_ids": ["dbi185ghoj4c73b5d1p0"],
                                 "backends": ["ibm_fez"]}
    mixed = card({"a": {"is_real_hardware": True}, "b": [{"is_real_hardware": False}]})
    assert mixed["provenance"]["is_real_hardware"] == "mixed"


def test_a_long_list_does_not_truncate_a_mixed_claim_into_a_true_one():
    """A capped walk would stop before the denial and report True. The walk is not capped."""
    payload = {"rows": [{"n": i} for i in range(900)] + [{"is_real_hardware": False}],
               "is_real_hardware": True}
    assert provenance(payload)["is_real_hardware"] == "mixed"


def test_the_callers_provenance_widens_and_never_overrides():
    """The adapter records the same thing under `quantum_backends`; the card's key is `backends` either way, and the
    union is taken so neither side can hide a job id the other saw."""
    spec = card({"quantum_backend": "aer_simulator"},
                meta={"provenance": {"is_real_hardware": False, "ibm_job_ids": ["j9"],
                                     "quantum_backends": ["cudaq_nvidia_A100"]}})
    assert spec["provenance"] == {"is_real_hardware": False, "ibm_job_ids": ["j9"],
                                  "backends": ["aer_simulator", "cudaq_nvidia_A100"]}


# --------------------------------------------------------------------------- rule 3: no invented verdict
def test_a_verdict_is_never_invented():
    bare = card(ADMET)
    assert bare["verdict"] is None and bare["findings"] == []
    carried = card(ADMET, meta={"verdict": "contradicted",
                                "findings": [{"severity": "high", "code": "quantum_twin", "path": "$"}]})
    assert carried["verdict"] == "contradicted"
    assert carried["findings"][0]["code"] == "quantum_twin"
    # and nothing in the card says "clean" on its own account
    assert "clean" not in json.dumps(bare)


def test_a_verdict_that_is_not_a_string_is_not_a_verdict():
    for bad in (True, 1, {"v": "clean"}, [], ""):
        assert card(ADMET, meta={"verdict": bad})["verdict"] is None
    assert card(ADMET, meta={"findings": "lots"})["findings"] == []


# --------------------------------------------------------------------------- rule 4: units
def test_a_number_keeps_the_unit_the_payload_states():
    spec = card({"delta_g_bind_kcal_mol": -9.4, "kd_nM": 12.0, "bsa_A2": 940.0, "latency_ms": 21.0,
                 "rmsd": 1.2, "half_life": 6.0,
                 "potency": {"value": 3.4, "units": "nM", "method": "bioactivity prediction"},
                 "exposure": 88.0, "exposure_units": "ng/mL"})
    rows = {r["label"]: r for b in blocks_of(spec, "facts") for r in b["rows"]}
    assert rows["delta_g_bind_kcal_mol"]["unit"] == "kcal/mol"
    assert rows["kd_nM"]["unit"] == "nM"
    assert rows["bsa_A2"]["unit"] == "Å²"
    assert rows["latency_ms"]["unit"] == "ms"
    assert rows["potency"]["unit"] == "nM" and rows["potency"]["value"] == 3.4
    assert rows["exposure"]["unit"] == "ng/mL"
    # the two the frontend's METRIC_GLOSSARY would have GUESSED (kd -> M, half_life -> h) carry no unit here
    assert "unit" not in rows["rmsd"] and "unit" not in rows["half_life"]


def test_a_unit_suffix_is_matched_case_sensitively():
    """`_nM` is nanomolar and `_nm` is nanometres; `length_aa` is residues and must not read as angstrom."""
    rows = {r["label"]: r for b in blocks_of(card({"gap_nm": 1.0, "ic50_nM": 1.0, "length_aa": 285}), "facts")
            for r in b["rows"]}
    assert rows["gap_nm"]["unit"] == "nm" and rows["ic50_nM"]["unit"] == "nM"
    assert rows["length_aa"]["unit"] == "aa"


def test_prose_never_carries_a_unit():
    rows = {r["label"]: r for b in blocks_of(card({"note_nt": "sequence trimmed", "reason_ms": "timeout"}), "facts")
            for r in b["rows"]}
    assert "unit" not in rows["note_nt"] and "unit" not in rows["reason_ms"]


# --------------------------------------------------------------------------- the recorded local answers
def test_smiles_to_3d_becomes_a_molecule3d_block():
    spec = card(SMILES_TO_3D, tool="smiles_to_3d")
    assert spec["kind"] == "molecule"
    assert blocks_of(spec, "molecule3d")[0]["sdf"] == SMILES_TO_3D["mol"]
    assert "CC(=O)Nc1ccc(O)cc1" in spec["title"]
    check_schema(spec)


def test_render_2d_becomes_a_molecule2d_block_only_while_the_format_says_png():
    spec = card(RENDER_2D, tool="render_2d")
    assert blocks_of(spec, "molecule2d")[0]["png_base64"] == RENDER_2D["image_base64"]
    other = card({"image_base64": "PHN2Zz4", "format": "svg"}, tool="render_2d")
    assert not blocks_of(other, "molecule2d")
    assert absent_about(other, "molecule2d")[0]["why"].count("svg") == 1
    check_schema(other)


def test_pdb_text_is_not_offered_as_sdf():
    """`structure_3d.pdb` is protein text; this block type carries SDF. Calling it SDF would hand the window a file
    it cannot open and label it a structure."""
    spec = card({"mol": "HEADER    TEST\nATOM      1  N   MET A   1      0.0 0.0 0.0\nEND\n", "plddt": 91.2})
    assert not blocks_of(spec, "molecule3d")
    assert "SDF only" in absent_about(spec, "molecule3d")[0]["why"]
    assert any("ATOM" in b["text"] for b in blocks_of(spec, "text"))
    check_schema(spec)


def test_the_motif_catalogue_becomes_a_table_and_keeps_its_total():
    spec = card(MOTIFS, tool="motifs_list")
    assert spec["kind"] == "catalogue"
    table = blocks_of(spec, "table")[0]
    assert table["columns"] == ["category", "entries"]
    assert sorted(table["rows"]) == [["alkyls", 2], ["aromatics", 1]]
    rows = {r["label"]: r["value"] for b in blocks_of(spec, "facts") for r in b["rows"]}
    assert rows["total_motifs"] == 120


def test_a_receipt_becomes_a_job_card_and_the_poll_route_is_a_fact_not_a_link():
    """Studio follows the allowlist's route, never one a body names, so a relative `poll_url` is never offered as
    something to click."""
    spec = card(RECEIPT, tool="pipeline_submit")
    assert spec["kind"] == "job"
    rows = {r["label"]: r["value"] for b in blocks_of(spec, "facts") for r in b["rows"]}
    assert rows["job_id"] == "5a362e56-077" and rows["status"] == "accepted"
    assert rows["poll_url"] == "/api/pipeline/job/5a362e56-077"
    assert not blocks_of(spec, "links")


def test_only_an_absolute_url_becomes_a_link():
    spec = card({"downloads": {"files": {"design.pdb": "https://example.invalid/d.pdb",
                                         "notes.txt": "/local/route"},
                               "session_id": "s1"}})
    items = blocks_of(spec, "links")[0]["items"]
    assert items == [{"label": "design.pdb", "url": "https://example.invalid/d.pdb"}]


# --------------------------------------------------------------------------- rule 5: the fallback
def test_a_tool_never_seen_before_still_carries_its_data():
    payload = {"pipeline": "chronobiology_resonance_v9",
               "stages": {"1_entrain": {"period_hours": 23.6, "amplitude": 0.44},
                          "2_phase": {"shift_hours": None}},
               "weird_scalar": 7, "weird_list": ["a", "b"],
               "rows": [{"sample": "s1", "delta": 0.2}, {"sample": "s2", "delta": 0.4}]}
    spec = card(payload, tool="a_tool_studio_has_never_heard_of")
    check_schema(spec)
    flat = json.dumps(spec)
    for name in ("period_hours", "amplitude", "weird_scalar", "weird_list", "sample", "delta",
                 "chronobiology_resonance_v9"):
        assert name in flat, name
    assert absent_about(spec, "shift_hours"), "the one null is drawn, not dropped"


def test_the_fallback_never_returns_zero_blocks():
    for payload in (None, {}, [], "", 0, False, {"only": None}, [{}], {"a": []}):
        spec = card(payload)
        assert spec["blocks"], payload
        check_schema(spec)


def test_every_leaf_of_an_unknown_payload_is_findable_in_the_card():
    payload = {"alpha": 1, "beta": "two", "gamma": {"delta": 3.5, "epsilon": "five"},
               "zeta": [{"eta": 6}, {"eta": 7}]}
    flat = json.dumps(card(payload))
    for path, value in leaves(payload):
        assert _leaf_name(path) in flat, path
        assert str(value) in flat or json.dumps(value) in flat, (path, value)


def _leaf_name(path):
    return path.rsplit(".", 1)[-1].split("[")[0]


def test_the_tool_name_informs_the_subtitle_and_nothing_else():
    """Discovery is by shape. A tool that lies about what it is cannot change a kind or a block."""
    honest = card(ADMET, tool="admet_standalone")
    liar = card(ADMET, tool="ibm_heron_real_hardware_vqe")
    assert honest["kind"] == liar["kind"] == "admet"
    assert honest["blocks"] == liar["blocks"]
    assert honest["provenance"] == liar["provenance"]
    assert "ibm_heron_real_hardware_vqe" in liar["subtitle"]
    assert liar["provenance"]["is_real_hardware"] is None


def test_the_payload_is_not_mutated():
    before = json.dumps(MOTIFS, sort_keys=True)
    card(MOTIFS, tool="motifs_list")
    assert json.dumps(MOTIFS, sort_keys=True) == before


# --------------------------------------------------------------------------- rule 6: the caps announce themselves
def test_the_table_cap_says_how_many_rows_were_left_out():
    spec = card({"candidates": [{"id": i, "score": i / 1000} for i in range(MAX_TABLE_ROWS + 1)]})
    table = blocks_of(spec, "table")[0]
    assert len(table["rows"]) == MAX_TABLE_ROWS
    said = [b["text"] for b in blocks_of(spec, "text") if "not shown" in b["text"]]
    assert any(f"1 of {MAX_TABLE_ROWS + 1} rows" in t for t in said), said


def test_exactly_at_the_table_cap_nothing_is_announced():
    spec = card({"candidates": [{"id": i, "score": i / 1000} for i in range(MAX_TABLE_ROWS)]})
    assert len(blocks_of(spec, "table")[0]["rows"]) == MAX_TABLE_ROWS
    assert not [b for b in blocks_of(spec, "text") if "not shown" in b["text"]]


def test_the_facts_cap_says_how_many_were_left_out():
    spec = card({f"field_{i}": i for i in range(MAX_FACTS + 1)})
    assert sum(len(b["rows"]) for b in blocks_of(spec, "facts")) == MAX_FACTS
    assert any(f"1 of {MAX_FACTS + 1} facts are not shown" in b["text"] for b in blocks_of(spec, "text"))


def test_exactly_at_the_facts_cap_nothing_is_announced():
    spec = card({f"field_{i}": i for i in range(MAX_FACTS)})
    assert sum(len(b["rows"]) for b in blocks_of(spec, "facts")) == MAX_FACTS
    assert not [b for b in blocks_of(spec, "text") if "not shown" in b["text"]]


def test_the_series_cap_says_how_many_points_were_left_out():
    spec = card({"iterations": [{"iteration": i, "convergence": i / 10000.0}
                                for i in range(MAX_SERIES_POINTS + 5)]})
    s = blocks_of(spec, "series")[0]
    assert s["x_label"] == "iteration" and len(s["points"]) == MAX_SERIES_POINTS
    assert any(f"5 of {MAX_SERIES_POINTS + 5} points" in b["text"] for b in blocks_of(spec, "text"))
    check_schema(spec)


def test_an_iteration_series_carries_the_numbers_the_payload_wrote():
    """ChatMessage's own convergence view reads `iterations[].iteration` and `.convergence`, and refuses to coerce a
    missing convergence to 0 because "0% converged" reads as a measurement."""
    spec = card({"optimization": {"iterations": [{"iteration": 1, "convergence": 0.25},
                                                 {"iteration": 2, "convergence": 0.75}]}})
    s = blocks_of(spec, "series")
    assert s and s[0]["points"] == [[1, 0.25], [2, 0.75]]
