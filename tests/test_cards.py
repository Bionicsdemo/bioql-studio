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
        "admet_quantum": {
            "backend": "Bionics LightGBM (classical ML)",
            "quantum_engine": "classical LightGBM ensemble (no quantum hardware in this prediction)",
            "raw_predictions": {"ames": 0.1337},
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
           "molecule3d": {"sdf", "parts", "label"}, "radar": {"axes", "source", "max"}, "series": {"x_label", "y_label", "points"}, "text": {"text"},
           "links": {"items"}, "absent": {"what", "why"},
           "chart_image": {"png_base64", "title", "url"},
           "status": {"word", "tone", "where", "counts", "issues", "issues_total"},
           "gates": {"rows", "where"}}


def check_schema(spec):
    """The contract, checked field by field: exact top-level keys, a known kind, and only the eight block types with
    only their own keys. Run over every fixture and over every junk payload, because a spec the window cannot read
    is the same failure as a wrong number."""
    # `section` es opcional: una tarjeta que vino de una seccion del resultado lleva cual, y nada mas cambia.
    assert set(spec) - {"section"} == {"kind", "title", "subtitle", "provenance", "verdict", "findings",
                                       "blocks"}
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


def test_a_chart_image_is_not_passed_off_as_the_molecule():
    """`visualization.charts[].image_base64` is a ranking plot. It gets its own block and `molecule2d` stays what
    it says it is: drawing the plot where the compound belongs would label a bar chart a structure."""
    spec = card({"smiles": "CCO",
                 "visualization": {"charts": [{"title": "ranking", "image_base64": "iVBORw0KGgo"}]}})
    two_d = blocks_of(spec, "molecule2d")
    assert two_d and "png_base64" not in two_d[0] and two_d[0]["smiles"] == "CCO"
    assert blocks_of(spec, "chart_image")[0]["title"] == "ranking"
    check_schema(spec)


def test_a_key_that_contradicts_its_own_text_is_refused():
    """`mol` says SDF and the text is PDB. The format is read off the KEY — BioQL named it — and then CHECKED:
    a structure handed to a viewer under the wrong format is drawn wrong, which is worse than not drawn."""
    spec = card({"mol": "HEADER    TEST\nATOM      1  N   MET A   1      0.0 0.0 0.0\nEND\n", "plddt": 91.2})
    assert not blocks_of(spec, "molecule3d")
    why = absent_about(spec, "molecule3d (molecule)")[0]["why"]
    assert "named sdf" in why and "does not read as sdf" in why
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


def test_a_declared_artifact_is_a_link_and_a_bare_route_is_not():
    """BioQL's own `ArtifactDownloads` keeps `downloads.files` values relative (`/api/...`, resolved against the
    backend) and still renders them as links, so an absolute-only rule would make this block never fire on a real
    payload. A `*_url` anywhere else is a route, and only an absolute one is offered: `poll_url` stays a fact,
    because Studio follows the allowlist's route and never one a body names."""
    spec = card({"downloads": {"files": {"design.pdb": "/api/download/design.pdb",
                                         "report.html": "https://example.invalid/r.html"},
                               "session_id": "s1"},
                 "poll_url": "/api/pipeline/job/x", "report_url": "https://example.invalid/report"})
    urls = {i["label"]: i["url"] for i in blocks_of(spec, "links")[0]["items"]}
    assert urls == {"design.pdb": "/api/download/design.pdb",
                    "report.html": "https://example.invalid/r.html",
                    "report_url": "https://example.invalid/report"}
    rows = {r["label"]: r["value"] for b in blocks_of(spec, "facts") for r in b["rows"]}
    assert rows["poll_url"] == "/api/pipeline/job/x"
    assert rows["downloads.session_id"] == "s1"


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
    assert honest["title"] == liar["title"]
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
    assert any(f"1 of {MAX_FACTS + 1} facts" in b["text"] and "not shown" in b["text"]
               for b in blocks_of(spec, "text"))


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


# --------------------------------------------------------------------------- depth: nothing is lost in silence
def test_a_twelve_stage_pipeline_five_levels_deep_loses_nothing():
    """The shape BioQL's own registry gates on is `stages.<n>.<field>`, and a hand-unrolled two-level pass loses the
    third. Every leaf is either in the card or counted in a block that says it was cut."""
    payload = {"pipeline": "deep_v1",
               "stages": {f"{i}_stage": {"metrics": {"inner": {"deepest": {"value_kcal_mol": -i}}},
                                         "ok": True, "name": f"s{i}"} for i in range(1, 13)}}
    spec = card(payload, tool="deep_tool")
    check_schema(spec)
    flat = json.dumps(spec)
    for i in range(1, 13):
        assert f"{i}_stage" in flat, i
    rows = {r["label"]: r for b in blocks_of(spec, "facts") for r in b["rows"]}
    cut = " ".join(b["text"] for b in blocks_of(spec, "text"))
    for i in range(1, 13):
        label = f"stages.{i}_stage.metrics.inner.deepest.value_kcal_mol"
        assert label in rows or "not shown" in cut, label
    assert rows["stages.1_stage.metrics.inner.deepest.value_kcal_mol"]["unit"] == "kcal/mol"


def test_a_list_nested_two_dicts_down_is_drawn():
    spec = card({"a": {"b": [1, 2, 3]}, "c": {"d": {"e": {"f": "g"}}}})
    flat = json.dumps(spec)
    assert "a.b" in flat and "1, 2, 3" in flat and "c.d.e.f" in flat and "g" in flat
    check_schema(spec)


def test_a_long_list_of_scalars_says_how_many_it_cut():
    spec = card({"smiles_list": [f"C{i}" for i in range(60)]})
    assert any("20 of 60 entries" in b["text"] for b in blocks_of(spec, "text"))
    check_schema(spec)


def test_a_nested_honest_value_is_one_absent_block_with_its_reason():
    """Two blocks for one field — one with BioQL's reason, one saying "no reason given" — is the degradation this
    module exists to stop, one level down."""
    spec = card({"pk": {"half_life": {"value": None, "null_reason": "no hepatocyte model"}}})
    hits = absent_about(spec, "half_life")
    assert len(hits) == 1 and hits[0]["why"] == "no hepatocyte model"
    assert not absent_about(spec, "null_reason")
    check_schema(spec)


def test_a_nested_honest_value_keeps_its_unit_and_its_method():
    spec = card({"pk": {"half_life": {"value": 6.2, "units": "h", "method": "PK predictor",
                                      "is_estimate": True, "ci90": [4.0, 9.1]}}})
    row = [r for b in blocks_of(spec, "facts") for r in b["rows"] if r["label"] == "pk.half_life"][0]
    assert row["value"] == 6.2 and row["unit"] == "h"
    # the three fields that say whether the number is measured or guessed travel with it
    assert "PK predictor" in row["note"] and "estimate" in row["note"] and "ci90 4.0" in row["note"]
    assert not [r for b in blocks_of(spec, "facts") for r in b["rows"] if r["label"].endswith("units")]
    check_schema(spec)


def test_an_orphan_units_key_is_still_data():
    spec = card({"exposure_units": "ng/mL"})
    rows = {r["label"]: r["value"] for b in blocks_of(spec, "facts") for r in b["rows"]}
    assert rows["exposure_units"] == "ng/mL"


# --------------------------------------------------------------------------- detectors are not gated on the kind
def test_a_pipeline_carrying_a_structure_still_gets_the_structure():
    """`scoring: {"admet": 0.3}` is a WEIGHT, not an ADMET result; reading it as one once turned a 12-stage pipeline
    into an ADMET card with no structure in it. Every detector runs, whatever the kind says."""
    payload = {"pipeline": "unified_drug_development", "stages": {"2_admet": {"data": {"logp": 1.2}}},
               "scoring": {"admet": 0.3, "docking": 0.7},
               "mol_3d": "\n  RDKit 3D\n\n  1  0  0  0  0  0  0  0  0  0999 V2000\nM  END\n"}
    spec = card(payload)
    assert spec["kind"] == "pipeline"
    assert blocks_of(spec, "molecule3d"), "the structure is drawn even though the kind is not 'molecule'"
    rows = {r["label"]: r["value"] for b in blocks_of(spec, "facts") for r in b["rows"]}
    assert rows["scoring.admet"] == 0.3 and rows["stages.2_admet.data.logp"] == 1.2
    check_schema(spec)


def test_a_pipeline_name_alone_does_not_make_a_pipeline_card():
    """The ADMET envelope carries `pipeline: "ADMET_STANDALONE"` as a NAME. Reading a name as a shape is the
    dispatch-by-name this module refuses; the name titles the card and the shape decides the kind."""
    assert card(ADMET)["kind"] == "admet"
    assert "ADMET_STANDALONE" in card(ADMET)["title"]


def test_the_quantum_twin_is_drawn_too_and_claims_nothing():
    """The real run carried an identical LightGBM payload under both `_classical` and `_quantum`. The card draws
    both — hiding one would hide the defect — and the quantum NAME still buys no provenance."""
    spec = card(ADMET)
    said = " ".join(b["text"] for b in blocks_of(spec, "text"))
    assert "admet_classical.raw_predictions" in said and "admet_quantum.raw_predictions" in said
    assert spec["provenance"]["is_real_hardware"] is None


def test_the_real_pdb_keys_are_recognised_and_capped():
    """`structure_3d.pdb`, `pdb_data` and `complex_pdb` are the keys the registry gates on. PDB text is not SDF, and
    a 40 000-character file is not a fact."""
    pdb = "HEADER    X\n" + "ATOM      1  N   MET A   1       0.0   0.0   0.0\n" * 900
    spec = card({"structure_3d": {"pdb": pdb, "plddt": 91.2, "length_aa": 285}})
    assert spec["kind"] == "structure"
    b = blocks_of(spec, "molecule3d")[0]
    assert [(x["role"], x["format"]) for x in b["parts"]] == [("protein", "pdb")]
    # entera: una estructura recortada es otra molecula
    assert b["parts"][0]["data"] == pdb and b["parts"][0]["bytes"] == len(pdb)
    rows = {r["label"]: r for b in blocks_of(spec, "facts") for r in b["rows"]}
    assert rows["structure_3d.plddt"]["value"] == 91.2 and rows["structure_3d.length_aa"]["unit"] == "aa"
    check_schema(spec)


def test_the_catalogue_keeps_its_entries_and_its_title():
    spec = card(MOTIFS, tool="motifs_list")
    assert spec["title"] == "Catalogue", "a card is never titled after the first entry of a nested catalogue"
    flat = json.dumps(spec)
    for name in ("methyl", "ethyl", "phenyl"):
        assert name in flat, name


def test_a_bare_chain_letter_is_not_an_angstrom():
    """`contacts_chain_A` is a chain. A suffix that can mean something other than a unit is not a unit."""
    rows = {r["label"]: r for b in blocks_of(card({"contacts_chain_A": 12, "bsa_A2": 940.0}), "facts")
            for r in b["rows"]}
    assert "unit" not in rows["contacts_chain_A"]
    assert rows["bsa_A2"]["unit"] == "\u00c5\u00b2"


def test_the_callers_provenance_cannot_turn_mixed_into_true():
    """The adapter's own walk caps long lists at 500, so it can see one claim where this walk sees two that
    disagree. Letting it override would turn a contradiction into a clean `true`."""
    payload = {"rows": [{"n": i} for i in range(900)] + [{"is_real_hardware": False}], "is_real_hardware": True}
    spec = card(payload, meta={"provenance": {"is_real_hardware": True, "ibm_job_ids": [], "backends": []}})
    assert spec["provenance"]["is_real_hardware"] == "mixed"
    other = card({"is_real_hardware": True, "ibm_job_id": "j1"},
                 meta={"provenance": {"is_real_hardware": "mixed", "ibm_job_ids": []}})
    assert other["provenance"]["is_real_hardware"] == "mixed"


def test_a_link_scheme_can_never_become_a_script():
    """A link is drawn as an anchor, so the scheme decides whether it is a download or a script. The window refuses
    these again: two barriers, because the payload comes from the engine and the window makes it clickable."""
    from studio import cards as c
    out = c.card({"downloads": {"files": {
        "js": "javascript:alert(1)", "data": "data:text/html,<script>1</script>", "vb": "VBScript:x",
        "proto": "//evil.example/x", "back": "/\\evil.example/x", "space": "/a b",
        "rel": "/api/reports/x.pdf", "abs": "https://example.org/a.pdf"}}})
    urls = [i["url"] for b in out["blocks"] if b["type"] == "links" for i in b["items"]]
    assert urls == ["/api/reports/x.pdf", "https://example.org/a.pdf"], urls
    assert not any(u.lower().startswith(("javascript:", "data:", "vbscript:", "//")) for u in urls)


def test_an_undeclared_url_must_be_absolute_http():
    from studio import cards as c
    out = c.card({"report_url": "/api/x"})
    assert [b for b in out["blocks"] if b["type"] == "links"] == []


# ------------------------------------------------- la estructura que BioQL devuelve de verdad: un par
REAL_COMPLEX = {
    "pipeline": "quantum_antibody_discovery",
    "mol_3d": {
        "ligand_sdf": "AB-KRAS-005\n  BioQL\n\n 17 16  0  0  0  0  0  0  0  0999 V2000\nM  END\n",
        "protein_pdb": "ATOM      1  N   GLY A   0      25.852  11.0  3.0\n" * 40,
    },
}


def test_a_ligand_and_its_protein_are_one_structure():
    """Medido en un resultado real (job 10620ccd-07b): BioQL no devuelve un fichero, devuelve un par —
    `mol_3d.ligand_sdf` y `mol_3d.protein_pdb`— que es lo que su propia ventana le da al visor. Llevando solo SDF
    se perdia la proteina y el ligando se anunciaba como "SDF only": la tarjeta se quedaba sin estructura."""
    spec = card(REAL_COMPLEX, tool="quantum_antibody_discovery")
    b = blocks_of(spec, "molecule3d")
    assert len(b) == 1, "un ligando dentro de su proteina es UNA figura, no dos tarjetas"
    assert [(p["role"], p["format"]) for p in b[0]["parts"]] == [("ligand", "sdf"), ("protein", "pdb")]
    assert b[0]["parts"][1]["data"] == REAL_COMPLEX["mol_3d"]["protein_pdb"]      # entera
    check_schema(spec)


def test_another_candidates_structure_is_named_not_merged():
    """Dos candidatos traen dos estructuras. Mezclarlas pondria el ligando de uno dentro de la proteina del otro."""
    payload = {"best_candidate": {"mol_3d": REAL_COMPLEX["mol_3d"]},
               "candidates": [{"mol_3d": REAL_COMPLEX["mol_3d"]}]}
    spec = card(payload)
    assert len(blocks_of(spec, "molecule3d")) == 1
    assert any("further structure(s)" in b["text"] for b in blocks_of(spec, "text"))
    check_schema(spec)


def test_a_structure_too_large_is_left_out_whole_and_said():
    import studio.cards as C
    big = "ATOM      1  N   GLY A   0      25.852  11.0  3.0\n" * 10
    old = C.MAX_STRUCT_BYTES
    C.MAX_STRUCT_BYTES = 50
    try:
        spec = card({"mol_3d": {"protein_pdb": big}})
    finally:
        C.MAX_STRUCT_BYTES = old
    assert not blocks_of(spec, "molecule3d")
    why = absent_about(spec, "molecule3d (protein)")[0]["why"]
    assert "never cut" in why and str(len(big)) in why
    check_schema(spec)


# ------------------------------------------------------------------ el radar de ADMET
def test_the_radar_copies_bioqls_own_scores_and_its_clamp():
    """Los ejes y el recorte a 0..1 son los de `ADMETRadarChart.tsx`, rama `radar_summary`."""
    spec = card({"admet": {"radar_summary": {"absorption": 0.82, "distribution": 0.4, "metabolism": 1.6,
                                             "excretion": 0.55, "safety": 0.9, "drug_likeness": 0.7,
                                             "solubility": 0.3, "selectivity": -2}}})
    r = blocks_of(spec, "radar")[0]
    vals = {a["label"]: a["value"] for a in r["axes"]}
    assert [a["label"] for a in r["axes"]][:3] == ["Absorption", "Distribution", "Metabolism"]
    assert vals["Metabolism"] == 1.0 and vals["Selectivity"] == 0.0        # el recorte, no un valor inventado
    assert vals["Absorption"] == 0.82
    check_schema(spec)


def test_an_axis_bioql_did_not_state_is_a_spoke_not_a_half():
    """`ADMETRadarChart.tsx` tiene una segunda rama que se fabrica la figura: una categoria sin numeros vale 0.5,
    un valor mayor que 1 "se asume" porcentaje, y las palabras high/moderate/low valen 0.8/0.5/0.2. Medido en dos
    resultados reales (10620ccd-07b y 5a362e56-077): NINGUNO trae `radar_summary`, asi que todo radar que :3000
    dibuja para ellos es esa fabricacion. Aqui un eje sin numero se queda sin vertice."""
    spec = card({"admet": {"radar_summary": {"absorption": 0.8, "distribution": 0.4, "metabolism": 0.6,
                                             "excretion": None, "safety": "alta", "drug_likeness": True}}})
    r = blocks_of(spec, "radar")[0]
    vals = {a["label"]: a["value"] for a in r["axes"]}
    assert vals["Excretion"] is None and vals["Safety"] is None
    assert vals["Drug-likeness"] is None, "un booleano no es una puntuacion medida"
    assert all(v != 0.5 for v in vals.values() if v is not None)
    why = absent_about(spec, "radar axes")[0]["why"]
    assert "Excretion" in why and "Safety" in why
    check_schema(spec)


def test_no_radar_summary_no_radar():
    """Los dos resultados reales no lo traen: la tarjeta no dibuja un radar en vez de dibujar uno inventado."""
    spec = card({"admet": {"absorption": {"hia": 0.9}, "distribution": {"ppb": 92.0}}})
    assert not blocks_of(spec, "radar")
    check_schema(spec)


# ------------------------------------------------- un resultado son las tarjetas que :3000 dibuja, no una sola
def test_a_result_becomes_the_sections_bioql_draws():
    """Las rutas salen de `cardRegistry.ts`, el manifiesto de BioQL. Medido en un resultado real
    (10620ccd-07b): su ventana dibuja 12 tarjetas y este modulo dibujaba UNA de 276 bloques."""
    from studio.cards import cards_of
    payload = {"pipeline": "quantum_antibody_discovery",
               "candidates": [{"id": "AB-1", "score": 0.8}],
               "admet": {"logp": 1.2},
               "toxicology": {"ames": 0.17},
               "pharmacology": {"half_life_days": 11.0},
               "clinical_trial": {"phase_I": {"n": 24}},
               "consistency_audit": {"status": "ok"},
               "algo_que_nadie_reclama": {"x": 1}}
    g = cards_of(payload, tool="quantum_antibody_discovery")
    assert g["kind"] == "group"
    titles = [c["title"] for c in g["cards"]]
    assert titles[:6] == ["Candidates", "ADMET", "Toxicology", "Pharmacology", "Clinical simulation",
                          "Consistency audit"]
    # nada se pierde entre secciones
    rest = [c for c in g["cards"] if c["section"] == "rest"][0]
    assert any("algo_que_nadie_reclama" in str(r.get("label")) for b in rest["blocks"]
               if b["type"] == "facts" for r in b["rows"])
    for c in g["cards"]:
        check_schema(c)


def test_one_section_is_still_one_card():
    """Una respuesta pequena no se envuelve en un grupo de uno."""
    from studio.cards import cards_of
    g = cards_of({"admet": {"logp": 1.2}}, tool="admet_standalone")
    assert g["kind"] != "group"


def test_a_section_is_read_where_bioql_reads_it():
    """La PRIMERA ruta del manifiesto que resuelve gana: `best_candidate.admet` no abre una segunda tarjeta ADMET
    al lado de `admet`."""
    from studio.cards import sections
    found = sections({"admet": {"a": 1}, "best_candidate": {"admet": {"b": 2}}, "docking": {"score": -8.1}})
    ids = [s[0] for s in found]
    assert ids.count("admet") == 1
    assert [s[2] for s in found if s[0] == "admet"] == ["admet"]


def test_a_zero_is_a_section_and_an_empty_dict_is_not():
    """`isNonEmpty` de BioQL: 0 y false son valores; null, "", [] y {} son ausencia."""
    from studio.cards import sections
    assert [s[0] for s in sections({"docking": 0})] == ["docking"]
    assert sections({"docking": {}}) == []


# ------------------------------------------------- lo que BioQL ya dibujo, su veredicto y sus puertas
def test_the_chart_bioql_already_drew_is_shown_not_refused():
    """`visualization.charts[]` es `{title, image_base64, png_url}` — dos en el resultado real 10620ccd-07b. Antes
    se rechazaban por no haber tipo de bloque; tirar el dibujo del motor para rehacerlo peor desde los numeros
    seria la tarjeta prefiriendose a si misma antes que al motor."""
    spec = card({"visualization": {"charts": [{"title": "Candidate Ranking", "image_base64": "iVBORw0KGgo=",
                                               "png_url": "/api/artifacts/antibody/chart_ranking.png"}]}})
    b = blocks_of(spec, "chart_image")[0]
    assert b["title"] == "Candidate Ranking" and b["png_base64"] == "iVBORw0KGgo="
    assert b["url"] == "/api/artifacts/antibody/chart_ranking.png"
    assert not blocks_of(spec, "molecule2d"), "un grafico no es una depiccion de la molecula"
    check_schema(spec)


def test_a_chart_url_to_another_origin_is_not_carried():
    spec = card({"visualization": {"charts": [{"title": "x", "image_base64": "aa", "png_url": "//otro/host.png"}]}})
    assert "url" not in blocks_of(spec, "chart_image")[0]
    check_schema(spec)


def test_the_audit_verdict_is_a_verdict_not_a_row():
    """`consistency_audit` contesta `{status:"pass", n_issues:0, n_critical:0}`: una frase sobre la corrida
    entera, que se perdia como una fila mas entre doscientas."""
    spec = card({"consistency_audit": {"status": "pass", "n_issues": 0, "n_critical": 0, "issues": []}})
    s = blocks_of(spec, "status")[0]
    assert s["word"] == "pass" and s["tone"] == "good"
    assert s["counts"] == {"n_issues": 0, "n_critical": 0}
    check_schema(spec)


def test_a_word_this_module_cannot_read_is_not_coloured_in():
    spec = card({"audit": {"status": "parcialmente revisado"}})
    assert blocks_of(spec, "status")[0]["tone"] == "plain"
    check_schema(spec)


def test_a_failed_audit_reads_as_failed_and_carries_its_issues():
    spec = card({"consistency_audit": {"status": "FAIL", "n_critical": 2,
                                       "issues": ["etiqueta cuantica sobre salida clasica", "sin brazo clasico"]}})
    s = blocks_of(spec, "status")[0]
    assert s["tone"] == "bad" and s["issues_total"] == 2
    assert "sin brazo clasico" in s["issues"]
    check_schema(spec)


def test_a_gate_carries_the_reason_bioql_gave():
    """`qdiffvision.capability_gates` empareja cada bandera con su `reason_<bandera>`. Dibujada como un true/false
    pelado se pierde justo la mitad que dice lo que vale la afirmacion."""
    spec = card({"qdiffvision": {"capability_gates": {
        "learned_se3_diffusion": True,
        "reason_learned_se3_diffusion": "validated graph-specific checkpoint; fixed topology only",
        "quantum_force_guidance": False,
        "reason_quantum_force_guidance": "not requested"}}})
    rows = {r["label"]: r for r in blocks_of(spec, "gates")[0]["rows"]}
    assert rows["learned_se3_diffusion"]["open"] is True
    assert "fixed topology only" in rows["learned_se3_diffusion"]["why"]
    assert rows["quantum_force_guidance"]["open"] is False
    assert rows["quantum_force_guidance"]["why"] == "not requested"
    check_schema(spec)


def test_bioqls_own_3dmol_embed_is_the_structure():
    """`visualization.molecular.embed_3dmol` es `{data, format:"pdb", style:"cartoon"}`: BioQL le da el formato al
    lado del texto, y es el unico sitio donde se lee de un hermano y no de la clave."""
    spec = card({"visualization": {"molecular": {"embed_3dmol": {
        "data": "ATOM      1  N   GLU A   1       4.714 -18.708 -12.472\n" * 20,
        "format": "pdb", "style": "cartoon"}}}})
    b = blocks_of(spec, "molecule3d")[0]
    assert [(p["role"], p["format"]) for p in b["parts"]] == [("protein", "pdb")]
    check_schema(spec)
