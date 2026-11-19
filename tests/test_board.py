"""The drug-design board, and the three adapter holes two agents found in it.

The board's job is not to be a picture: it is that a conclusion on it reaches the raw call, and that nothing can be
hung on it by hand. Both directions are tested.
"""
import pytest

from studio import board
from studio.adapter import provenance, wants_hardware

ROW = {"hash": "a" * 64, "tool": "admet_standalone", "state": "RESOLVED", "verdict": "contradicted",
       "findings": [{"severity": "high", "code": "quantum_twin", "detail": "…", "path": "$.x"}],
       "result_sha256": "b" * 64, "t": 1795100000.0,
       "provenance": {"is_real_hardware": None, "ibm_job_ids": [], "quantum_backends": ["NONE"]}}
CLEAN = {**ROW, "hash": "c" * 64, "verdict": "clean", "findings": [],
         "provenance": {"is_real_hardware": True, "ibm_job_ids": ["dbimbpo1"], "quantum_backends": ["ibm_fez"]}}


def test_a_number_cannot_be_hung_on_the_board_by_hand():
    """The rule that makes the board evidence rather than decoration."""
    b = board.new("GLP-1R")
    with pytest.raises(ValueError, match="ledger line"):
        board.add(b, "evidence", "binds at 2 nM")
    node = board.add(b, "evidence", "binds at 2 nM", ledger_hash="a" * 64)
    assert node["kind"] == "evidence"


def test_unknown_kinds_and_links_are_refused():
    b = board.new("x")
    with pytest.raises(ValueError):
        board.add(b, "conclusion", "we win")
    t = board.add(b, "target", "GLP-1R")
    with pytest.raises(ValueError):
        board.link(b, t["id"], "proves", t["id"])
    with pytest.raises(ValueError):
        board.link(b, t["id"], "supports", "n99")


def test_nothing_checked_it_is_unknown_not_unsupported():
    """A node nothing has looked at has not failed."""
    b = board.new("x")
    t = board.add(b, "target", "DNA-PKcs")
    s = board.support(b, t["id"])
    assert s["state"] == board.UNKNOWN and "nothing has checked it" in s["why"]


def test_contradicted_evidence_never_counts_as_support():
    b = board.new("x")
    t = board.add(b, "target", "GLP-1R")
    board.evidence_from(b, ROW, about=t["id"])          # verdict contradicted
    s = board.support(b, t["id"])
    assert s["state"] == board.UNKNOWN and s["not_counted"] and not s["supported_by"]
    assert "could not be counted" in s["why"]


def test_an_unresolved_run_never_counts_as_support():
    b = board.new("x")
    t = board.add(b, "target", "GLP-1R")
    board.evidence_from(b, {**CLEAN, "state": "UNRESOLVABLE", "verdict": None}, about=t["id"])
    assert board.support(b, t["id"])["state"] == board.UNKNOWN


def test_clean_evidence_supports_and_contradicting_evidence_wins():
    b = board.new("x")
    t = board.add(b, "target", "GLP-1R")
    board.evidence_from(b, CLEAN, about=t["id"])
    assert board.support(b, t["id"])["state"] == board.SUPPORTED
    board.evidence_from(b, {**CLEAN, "hash": "d" * 64}, about=t["id"], contradicts=True)
    assert board.support(b, t["id"])["state"] == board.UNSUPPORTED


def test_evidence_carries_its_provenance_real_or_null():
    b = board.new("x")
    n = board.evidence_from(b, ROW)
    assert n["ledger_hash"] == "a" * 64 and n["is_real_hardware"] is None and n["ibm_job_ids"] == []
    m = board.evidence_from(b, CLEAN)
    assert m["is_real_hardware"] is True and m["ibm_job_ids"] == ["dbimbpo1"]


def test_the_bad_news_is_at_the_top_of_the_page():
    b = board.new("GLP-1R agonist", "Can we design one without the pan-CYP liability?")
    t = board.add(b, "target", "GLP-1R")
    board.add(b, "risk", "pan-CYP liability")
    board.add(b, "plan", "fold, then dock, then ADMET")
    board.evidence_from(b, ROW, about=t["id"])
    md = board.markdown(b)
    head = md.split("## ")[0]
    assert "high finding" in head and "contradicted" in head
    r = board.report(b)
    assert r["high_findings"] == 1 and r["plans_awaiting_a_person"]
    assert "`aaaaaaaaaaaa`" in md                      # every row leads back to its call


def test_a_board_round_trips_and_a_corrupt_one_is_not_an_empty_one(studio_home):
    b = board.new("round trip")
    board.add(b, "target", "NRF2")
    board.save(b)
    again = board.load(b["id"])
    assert again.resolved and again.value["nodes"]
    assert board.load("nope").state == "ABSENT"
    board.path_of(b["id"]).write_text("{not json")
    bad = board.load(b["id"])
    assert bad.state == "UNRESOLVABLE"                 # never an empty board, which would read as "nothing on it"
    assert any(x.get("unreadable") for x in board.boards())


# --------------------------------------------------- the three adapter holes, closed
def test_a_nested_hardware_request_cannot_slip_past_the_gate():
    """`wants_hardware` read only top-level keys, so {"opts": {"real_qpu": true}} passed."""
    assert wants_hardware({"opts": {"real_qpu": True}})
    assert wants_hardware({"runs": [{"backend": "ibm_fez"}]})
    assert not wants_hardware({"opts": {"real_qpu": False}})
    assert not wants_hardware({"notes": {"backend_hint": "ask about ibm later"}})


def test_provenance_reads_the_plain_backend_key():
    """A report built from provenance could not name the device the body itself named."""
    assert provenance({"stages": {"s": {"backend": "ibm_fez"}}})["quantum_backends"] == ["ibm_fez"]


def test_a_body_cut_short_is_unresolvable_not_a_crash(fake_bioql, monkeypatch):
    """http.client.IncompleteRead is not an OSError, so a truncated streamed body escaped as a bare exception."""
    import http.client

    from studio import allowlist as al
    from studio.adapter import Adapter
    a = Adapter(fake_bioql, {k: al._norm(k, v) for k, v in al.DEFAULTS.items()})

    def torn(*_a, **_k):
        raise http.client.IncompleteRead(b"half a bo")

    monkeypatch.setattr(a, "open", torn)
    res = a.call("backend_health")
    assert res.state == "UNRESOLVABLE" and "could not reach" in res.detail


def test_a_verdict_never_recorded_is_not_read_as_clean():
    """A row whose line recorded no verdict says "not recorded". Reading that as clean is how an unchecked number
    becomes a conclusion. Found by building a real board from the ledger: two rows with no verdict were counting as
    support for the candidate."""
    b = board.new("x")
    c = board.add(b, "candidate", "paracetamol")
    board.evidence_from(b, {**CLEAN, "verdict": None}, about=c["id"])
    s = board.support(b, c["id"])
    assert s["state"] == board.UNKNOWN
    assert s["not_counted"][0]["why"] == "verdict not recorded"


def test_concerns_does_not_support_either():
    b = board.new("x")
    c = board.add(b, "candidate", "x")
    board.evidence_from(b, {**CLEAN, "verdict": "concerns"}, about=c["id"])
    assert board.support(b, c["id"])["state"] == board.UNKNOWN
