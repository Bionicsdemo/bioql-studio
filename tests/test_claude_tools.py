"""What Claude can do with Studio itself: the record, the findings, a big result in pieces, the whiteboard.

The point of these tools is that the brain stops being blind to the instrument around it, so what is tested is
exactly that: it can read what was already run, it can see what the verifier contradicted, it can read a 130 KB
result without pulling 130 KB into the conversation, and it CANNOT put a number on the board that has no call
behind it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from studio import claude_tools as ct
from studio import ledger
from studio.mcp_server import MAX_TEXT, tool_list


@pytest.fixture
def recorded(studio_home):
    rows = [
        {"tool": "backend_health", "state": "RESOLVED", "verdict": "clean", "by": "claude", "seconds": 0.1,
         "findings": []},
        {"tool": "peptide_discovery", "state": "RESOLVED", "verdict": "contradicted", "by": "window", "seconds": 9.0,
         "findings": [{"severity": "high", "code": "quantum_label_vs_execution",
                       "detail": "a quantum name over classical work", "path": "$.x"}]},
        {"tool": "admet_standalone", "state": "UNRESOLVABLE", "verdict": None, "by": "claude", "seconds": 0.2},
    ]
    for r in rows:
        ledger.append(r)
    return rows


def test_claude_can_read_what_has_already_been_run(recorded):
    out = ct.handle("studio_record", {"limit": 50}, None)
    assert [r["tool"] for r in out["rows"]][-3:] == ["backend_health", "peptide_discovery", "admet_standalone"]
    assert out["chain"]["ok"] is True
    # and filter it
    flagged = ct.handle("studio_record", {"verdict": "flagged"}, None)["rows"]
    assert [r["tool"] for r in flagged] == ["peptide_discovery"]
    one = ct.handle("studio_record", {"tool": "backend_health"}, None)["rows"]
    assert one and all(r["tool"] == "backend_health" for r in one)


def test_the_record_never_hands_claude_a_payload(studio_home):
    ledger.append({"tool": "big", "state": "RESOLVED", "verdict": "clean",
                   "value": {"a": "x" * 1000}, "args": {"smiles": "CCO"}})
    rows = ct.handle("studio_record", {}, None)["rows"]
    row = [r for r in rows if r.get("tool") == "big"][-1]
    assert row["value"] == "<dict>"          # the record holds hashes; the payload is read with studio_sections
    assert "args" not in row


def test_claude_sees_what_the_verifier_contradicted(recorded):
    out = ct.handle("studio_findings", {}, None)
    assert out["counted"] == 1
    f = out["findings"][0]
    assert f["tool"] == "peptide_discovery"
    assert f["findings"][0]["code"] == "quantum_label_vs_execution"
    assert f["findings"][0]["detail"], "a code with no explanation is nothing to act on"
    assert "contradicted ITSELF" in out["note"]      # and the note refuses to oversell what clean means
    everything = ct.handle("studio_findings", {"only_flagged": False}, None)
    assert everything["counted"] > out["counted"]


def test_usage_is_counted_off_the_record(recorded):
    out = ct.handle("studio_usage", {"days": 30}, None)
    assert out["calls"] >= 3
    assert out["by_verdict"]["contradicted"] == 1
    assert out["seconds_in_bioql"] >= 9.0
    assert "metered" in out["model_metering"] or "no model call" in out["model_metering"]


# ------------------------------------------------------------------ a big result, in pieces
BIG = {
    "result": {
        "consistency_audit": {"status": "pass", "n_issues": 0},
        "fep_rbfe_alchemical": {"engine": "fep_rbfe_alchemical", "is_real": True, "n": 18,
                                "metrics": {"rmse": 0.9}},
        "provenance_manifest": {"modules": [{"m": f"mod{i}", "sha256": "a" * 64} for i in range(400)]},
    }
}


def test_a_large_result_is_read_as_sections_not_as_itself():
    secs = ct.handle("studio_sections", {"payload": json.dumps(BIG)}, None)
    ids = [s["id"] for s in secs["sections"]]
    assert "fep" in ids and "consistencyAudit" in ids
    whole = secs["total_bytes"]
    assert whole > 20_000
    # the section costs a fraction of the whole, and the page says where the bulk actually is
    fep = [s for s in secs["sections"] if s["id"] == "fep"][0]
    assert fep["bytes"] * 20 < whole
    assert secs["biggest_keys"][0][0] == "provenance_manifest"


def test_one_section_comes_back_as_the_blocks_the_window_draws():
    out = ct.handle("studio_section", {"payload": json.dumps(BIG), "section": "fep"}, None)
    assert out["id"] == "fep" and out["shown_from"].startswith("fep_rbfe")
    assert isinstance(out["blocks"], list) and out["blocks"]
    bad = ct.handle("studio_section", {"payload": json.dumps(BIG), "section": "nope"}, None)
    assert "error" in bad and "fep" in bad["available"]


def test_an_answer_that_does_not_fit_is_described_and_never_cut():
    """The defect this replaces: `text[:200_000]` handed the brain JSON chopped mid-object with no reason."""
    real = json.dumps({"state": "RESOLVED", "source": "bioql", "detail": "HTTP 200",
                       "value": {"x": "y" * 300_000},
                       "meta": {"verdict": "contradicted", "seconds": 3.0,
                                "findings": [{"code": "quantum_label_vs_execution", "detail": "…", "path": "$"}],
                                "provenance": {"is_real_hardware": False, "ibm_job_ids": []}}})
    assert len(real) > MAX_TEXT
    said = json.loads(ct.too_big(real, MAX_TEXT))
    assert json.loads(ct.too_big(real, MAX_TEXT))            # above all: it still parses
    assert said["state"] == "RESOLVED" and said["verdict"] == "contradicted"
    assert said["findings"][0]["code"] == "quantum_label_vs_execution"
    assert said["provenance"]["is_real_hardware"] is False
    assert said["truncated"]["bytes"] == len(real)
    assert "studio_sections" in said["truncated"]["what_to_do"]
    assert "y" * 100 not in json.dumps(said)                 # and the payload is not smuggled in


# ------------------------------------------------------------------ the board
def test_claude_can_plan_on_the_board(studio_home):
    made = ct.handle("studio_board_write", {"act": "new", "title": "PD-L1", "question": "is it worth it?"}, None)
    bid = made["board"]
    node = ct.handle("studio_board_write", {"act": "node", "id": bid, "kind": "target", "label": "PD-L1"}, None)
    assert node["node"]["kind"] == "target"
    read = ct.handle("studio_board", {"id": bid}, None)
    assert read["board"]["title"] == "PD-L1" and read["report"]
    assert bid in [b["id"] for b in ct.handle("studio_board", {}, None)["boards"]]


def test_claude_cannot_put_evidence_on_the_board_without_a_call_behind_it(studio_home):
    """The one thing this door must refuse. Evidence names a LEDGER HASH and the row is read from the record."""
    bid = ct.handle("studio_board_write", {"act": "new", "title": "b"}, None)["board"]
    out = ct.handle("studio_board_write", {"act": "evidence", "id": bid, "ledger_hash": "f" * 64}, None)
    assert "not on the hash-chained record" in out["error"]

    row = ledger.append({"tool": "backend_health", "state": "RESOLVED", "verdict": "clean",
                         "provenance": {"is_real_hardware": None, "ibm_job_ids": []}})
    real = [r for r in ct.handle("studio_record", {}, None)["rows"] if r.get("tool") == "backend_health"][-1]
    ok = ct.handle("studio_board_write", {"act": "evidence", "id": bid, "ledger_hash": real["hash"]}, None)
    assert ok["node"]["kind"] == "evidence" and ok["node"].get("ledger_hash") == real["hash"]


def test_the_studio_tools_reach_bioql_through_nothing_of_their_own():
    """They read what Studio wrote down, or re-read a result through the SAME allowlisted route anyone would use.
    There is no second path to BioQL in this file."""
    src = Path(ct.__file__).read_text()
    for bad in ("urllib", "requests", "http.client", "socket", "subprocess", "os.system"):
        assert bad not in src, f"claude_tools.py mentions {bad}"
    assert 'ad.call("recovery_session_id_result"' in src     # the one call it makes, by allowlist name
