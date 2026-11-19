"""The signed notebook. A guard that cannot fail is the same defect it prevents, so most of this file is attacks.

The cheap attacks (a flipped byte, a swapped line) are caught by the hash and the link before any signature is read,
so they would also pass with the signature check stubbed to True. The tests that actually exercise Ed25519 are the
ones that repair the chain — `test_a_rehashed_chain_is_not_signed` and `test_transplanted_signatures_are_rejected` —
plus the forged `key_id`. Those three were run against a stubbed `_state_of` returning VALID and went red.
"""
import json
import os
from pathlib import Path

import pytest

from studio import ledger, notebook

ROW = {"tool": "admet_standalone", "method": "POST", "path": "/api/pipeline/submit",
       "args": {"smiles": "CC(=O)Nc1ccc(O)cc1"}, "state": "RESOLVED", "detail": "HTTP 200", "http": 200,
       "result_sha256": "a" * 64, "by": "claude", "approved_by": "the owner",
       "provenance": {"is_real_hardware": False, "ibm_job_ids": [], "quantum_backends": ["aer_simulator"]},
       "seconds": 4.2}


def _log(tmp_path) -> Path:
    return tmp_path / "runs.jsonl"


def _write(f: Path, rows: list[dict]) -> None:
    f.write_bytes(b"".join(ledger.canon(r) + b"\n" for r in rows))


def _rows(f: Path) -> list[dict]:
    return [json.loads(l) for l in f.read_bytes().splitlines()]


def _three(f: Path) -> list[dict]:
    return [notebook.record({**ROW, "n": i}, f) for i in range(3)]


# ----------------------------------------------------------------- the key
def test_the_key_is_created_once_private_and_is_never_replaced(studio_home):
    priv = notebook.device_key()
    kp, pp = notebook.key_path(), notebook.pub_path()
    assert oct(os.stat(kp).st_mode & 0o777) == "0o600" and oct(os.stat(pp).st_mode & 0o777) == "0o600"
    before = kp.read_text()
    assert notebook.device_key().private_bytes_raw() == priv.private_bytes_raw()
    assert kp.read_text() == before                              # a second use does not rotate the key
    assert notebook.key_id(notebook._pub_raw(priv)) == notebook.public_key()[1]
    assert len(notebook.public_key()[1]) == notebook.KEY_ID_LEN


def test_a_corrupt_key_raises_and_is_left_alone(studio_home):
    notebook.key_path().parent.mkdir(parents=True, exist_ok=True)
    notebook.key_path().write_text("not a key at all\n")
    with pytest.raises(notebook.CorruptKey):
        notebook.device_key()
    assert notebook.key_path().read_text() == "not a key at all\n"      # not regenerated behind the owner's back
    notebook.key_path().write_text("ab" * 20 + "\n")                    # valid hex, wrong length
    with pytest.raises(notebook.CorruptKey):
        notebook.device_key()


def test_a_missing_public_key_is_reported_not_guessed(studio_home, tmp_path):
    f = _log(tmp_path)
    _three(f)
    saved = notebook.pub_path().read_text()
    notebook.pub_path().unlink()
    r = notebook.verify(f)
    assert r["ok"] is False and r["broken_at"] is None and "no public key" in r["why"]
    # Its own state: three signed lines left UNCHECKED is not three lines signed by somebody else.
    assert r["unchecked_no_key"] == 3 and r["signed_by_other_key"] == 0 and r["unsigned"] == 0
    assert notebook.export(1, f)["signature"]["state"] == notebook.NO_KEY
    assert "UNCHECKED" in notebook.export(1, f)["text"]
    assert notebook.verify(f, pubkey=saved)["ok"] is True          # the key can still be supplied by hand


def test_a_corrupt_public_key_is_reported_not_raised(studio_home, tmp_path):
    """A public key that is not a key says nothing about the log: it is a log nobody here can check."""
    f = _log(tmp_path)
    _three(f)
    notebook.pub_path().write_text("deadbeef\n")
    r = notebook.verify(f)
    assert r["ok"] is False and r["broken_at"] is None and "cannot be read" in r["why"]


def test_a_lost_public_half_is_rebuilt_from_the_private_one(studio_home, tmp_path):
    f = _log(tmp_path)
    _three(f)
    notebook.pub_path().unlink()
    notebook.device_key()
    assert notebook.verify(f)["ok"] is True


def test_without_ed25519_it_says_so_instead_of_inventing_one(studio_home, monkeypatch):
    def no_crypto(name):
        raise ImportError("No module named 'cryptography'")
    monkeypatch.setattr(notebook, "_mod", no_crypto)
    with pytest.raises(notebook.NoCrypto) as why:
        notebook.device_key()
    assert "cryptography" in str(why.value)


# ----------------------------------------------------------------- recording and the happy path
def test_record_signs_every_line_and_the_ledger_stays_the_ledger(studio_home, tmp_path):
    f = _log(tmp_path)
    rows = _three(f)
    kid = notebook.public_key()[1]
    assert [r["seq"] for r in rows] == [1, 2, 3] and all(r["key_id"] == kid and len(r["sig"]) == 128 for r in rows)
    # the signature lives beside the line, so the ledger's own verifier is untouched
    assert "sig" not in _rows(f)[0] and ledger.verify(f)["ok"] is True
    assert oct(os.stat(notebook.sig_path(f)).st_mode & 0o777) == "0o600"
    r = notebook.verify(f)
    assert r == {"ok": True, "lines": 3, "broken_at": None, "why": None, "notes": None, "key_ids": {kid: 3},
                 "current_key_id": kid, "unsigned": 0, "signed_by_other_key": 0, "unchecked_no_key": 0,
                 "orphan_signatures": 0, "orphans_explained_by_a_repair": 0, "repairs_on_the_record": 0}


def test_an_empty_notebook_is_not_a_broken_one(studio_home, tmp_path):
    r = notebook.verify(_log(tmp_path))
    assert r["ok"] is True and r["lines"] == 0


def test_ledger_append_still_works_and_its_lines_are_unsigned_not_broken(studio_home, tmp_path):
    f = _log(tmp_path)
    notebook.record(ROW, f)
    ledger.append({"tool": "backend_health"}, f)          # the plain ledger, untouched and still usable
    notebook.record(ROW, f)
    r = notebook.verify(f)
    assert r["ok"] is False and r["broken_at"] is None and r["unsigned"] == 1 and "no signature" in r["why"]
    assert ledger.verify(f)["ok"] is True                 # the chain itself is intact: it is the proof that is missing


# ----------------------------------------------------------------- detection
def test_a_flipped_byte_is_detected(studio_home, tmp_path):
    f = _log(tmp_path)
    _three(f)
    rows = _rows(f)
    rows[1]["args"]["smiles"] = "CCO"                      # the parameters of run 2, rewritten
    _write(f, rows)
    r = notebook.verify(f)
    assert r["ok"] is False and r["broken_at"] == 2 and "hash" in r["why"]


def test_reordered_lines_are_detected(studio_home, tmp_path):
    f = _log(tmp_path)
    _three(f)
    rows = _rows(f)
    _write(f, [rows[0], rows[2], rows[1]])
    r = notebook.verify(f)
    assert r["ok"] is False and r["broken_at"] == 2 and "link" in r["why"]


def test_a_removed_middle_line_is_detected(studio_home, tmp_path):
    f = _log(tmp_path)
    _three(f)
    rows = _rows(f)
    _write(f, [rows[0], rows[2]])
    r = notebook.verify(f)
    assert r["ok"] is False and r["broken_at"] == 2


def test_a_truncated_tail_is_detected_by_its_orphaned_signature(studio_home, tmp_path):
    """Cutting whole lines off the end leaves the chain consistent: the ledger alone cannot see it. The signature
    file is keyed by hash, so the signature of the cut line is left with no line to belong to."""
    f = _log(tmp_path)
    _three(f)
    _write(f, _rows(f)[:2])
    assert ledger.verify(f)["ok"] is True                  # the chain is happy; it has been shortened
    r = notebook.verify(f)
    assert r["ok"] is False and r["broken_at"] is None and r["orphan_signatures"] == 1 and "truncated" in r["why"]


def test_a_torn_tail_is_reported_not_raised(studio_home, tmp_path):
    f = _log(tmp_path)
    _three(f)
    raw = f.read_bytes()
    f.write_bytes(raw[: -len(ledger.canon(_rows(f)[-1])) // 2])     # a crash mid-write
    r = notebook.verify(f)
    assert r["ok"] is False and r["broken_at"] == 3 and "JSON" in r["why"]


def test_a_rehashed_chain_is_not_signed(studio_home, tmp_path):
    """The real attack: change a line and recompute every hash and link after it. `ledger.verify` is satisfied —
    this is exactly what the signature is for."""
    f = _log(tmp_path)
    _three(f)
    rows = _rows(f)
    rows[0]["result_sha256"] = "b" * 64
    prev = "0" * 64
    for r in rows:
        r["prev"] = prev
        r.pop("hash")
        r["hash"] = prev = ledger.digest(r)
    _write(f, rows)
    assert ledger.verify(f)["ok"] is True                   # the chain was rebuilt and reads as clean
    got = notebook.verify(f)
    assert got["ok"] is False and got["unsigned"] == 3 and got["orphan_signatures"] == 3


def test_transplanted_signatures_are_rejected(studio_home, tmp_path):
    """The same attack, carried further: the old signatures are moved onto the new hashes so no line looks unsigned.
    Ed25519 is the only thing that catches this."""
    f = _log(tmp_path)
    signed = _three(f)
    rows = _rows(f)
    rows[0]["args"] = {"smiles": "CCO"}
    prev = "0" * 64
    for r in rows:
        r["prev"] = prev
        r.pop("hash")
        r["hash"] = prev = ledger.digest(r)
    _write(f, rows)
    notebook.sig_path(f).write_bytes(b"".join(
        ledger.canon({"hash": new["hash"], "key_id": old["key_id"], "sig": old["sig"]}) + b"\n"
        for new, old in zip(rows, signed)))
    got = notebook.verify(f)
    assert got["ok"] is False and got["broken_at"] == 1 and "signature" in got["why"]


def test_a_line_signed_by_another_key_is_reported_as_such(studio_home, tmp_path):
    """A key replacement is not "everything is bad": the verifier says which key, and how many lines, and those
    lines verify when that key's public half is handed to it."""
    f = _log(tmp_path)
    old_rows = _three(f)
    old_pub = notebook.pub_path().read_text()
    old_kid = notebook.public_key()[1]
    notebook.key_path().unlink()
    notebook.pub_path().unlink()
    notebook.record(ROW, f)                                # a new device key signs line 4
    new_kid = notebook.public_key()[1]
    assert new_kid != old_kid

    r = notebook.verify(f)
    assert r["ok"] is False and r["broken_at"] is None     # unverifiable, NOT judged bad
    assert r["signed_by_other_key"] == 3 and r["unsigned"] == 0
    assert r["key_ids"] == {new_kid: 1, old_kid: 3} and old_kid in r["why"] and "another key" in r["why"]

    with_old = notebook.verify(f, pubkey=old_pub)          # the old key checks its own three lines
    assert with_old["key_ids"][old_kid] == 3 and with_old["signed_by_other_key"] == 1
    assert with_old["broken_at"] is None
    assert [x["key_id"] for x in old_rows] == [old_kid] * 3


def test_a_forged_key_id_is_a_bad_signature_not_a_key_replacement(studio_home, tmp_path):
    """A foreign key signing while claiming this device's key_id is a forgery, and must read differently from an
    honest replacement: BAD (broken_at), not OTHER_KEY (unverifiable)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    f = _log(tmp_path)
    _three(f)
    kid = notebook.public_key()[1]
    rows, foreign = _rows(f), Ed25519PrivateKey.generate()
    notebook.sig_path(f).write_bytes(b"".join(
        ledger.canon({"hash": r["hash"], "key_id": kid,
                      "sig": foreign.sign(notebook._message(r["hash"], kid)).hex()}) + b"\n" for r in rows))
    got = notebook.verify(f)
    assert got["ok"] is False and got["broken_at"] == 1 and "does not match" in got["why"]


def test_a_run_deleted_and_re_signed_with_the_device_key_leaves_a_gap_in_seq(studio_home, tmp_path):
    """Whoever deletes a run and HAS the device key rebuilds the chain and signs it properly: hash, link and
    signature all come out correct, and `seq` is the only thing left that says a run is missing — which is why it
    goes in before the line is hashed.

    It is not a guarantee: the same person can renumber `seq` in the same pass and nothing here will see it (the
    module's docstring says so). This catches the one who did not bother."""
    f = _log(tmp_path)
    _three(f)
    rows = [r for r in _rows(f) if r["seq"] != 2]          # run 2 never happened, as far as the file shows
    prev = "0" * 64
    for r in rows:
        r["prev"] = prev
        r.pop("hash")
        r["hash"] = prev = ledger.digest(r)
    _write(f, rows)
    notebook.sig_path(f).write_bytes(b"".join(ledger.canon({k: s[k] for k in ("hash", "key_id", "sig")}) + b"\n"
                                              for s in (notebook.sign_line(r) for r in rows)))
    assert ledger.verify(f)["ok"] is True                  # the chain and every signature are genuine
    got = notebook.verify(f)
    assert got["ok"] is False and got["broken_at"] == 2 and "seq 3" in got["why"]


def test_a_corrupt_signature_file_is_reported(studio_home, tmp_path):
    f = _log(tmp_path)
    _three(f)
    p = notebook.sig_path(f)
    p.write_bytes(p.read_bytes() + b"{not json\n")
    got = notebook.verify(f)
    assert got["ok"] is False and "not a usable signature" in got["why"]


def test_record_under_threads_does_not_fork_the_chain(studio_home, tmp_path, monkeypatch):
    """Two threads reading the same `seq` would write two lines claiming one position and brick an honest log
    forever. `_next_seq` is slowed here so the race is taken, not hoped for."""
    import threading
    import time as _t
    f = _log(tmp_path)
    notebook.device_key()                                   # created once, outside the race
    real = notebook._next_seq
    monkeypatch.setattr(notebook, "_next_seq", lambda p: (_t.sleep(0.02), real(p))[1])
    ts = [threading.Thread(target=notebook.record, args=({**ROW, "i": i}, f)) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert [r["seq"] for r in _rows(f)] == [1, 2, 3, 4, 5, 6]
    assert notebook.verify(f) == {**notebook.verify(f), "ok": True, "lines": 6}


def test_a_caller_cannot_choose_its_own_position(studio_home, tmp_path):
    f = _log(tmp_path)
    notebook.record(ROW, f)
    assert notebook.record({**ROW, "seq": 999}, f)["seq"] == 2


def test_signing_an_unhashed_row_is_refused(studio_home):
    with pytest.raises(ValueError):
        notebook.sign_line({"tool": "x"})


# ----------------------------------------------------------------- the entry a reader is handed
def test_export_carries_parameters_result_provenance_chain_and_signature(studio_home, tmp_path):
    f = _log(tmp_path)
    notebook.record({**ROW, "verdict": "clean", "findings": []}, f)
    notebook.record({**ROW, "tool": "sqd_forces", "provenance": {"is_real_hardware": True,
                                                                 "ibm_job_ids": ["dbi185ghoj4c73b5d1p0"],
                                                                 "quantum_backends": ["ibm_fez"]},
                     "verdict": "contradicted",
                     "findings": [{"severity": "high", "code": "quantum_twin", "path": "$.a", "detail": "same payload"}]}, f)
    e = notebook.export(2, f)
    assert e["tool"] == "sqd_forces" and e["parameters"] == ROW["args"] and e["result_sha256"] == "a" * 64
    assert e["provenance"] == {"recorded": True, "is_real_hardware": True,
                               "ibm_job_ids": ["dbi185ghoj4c73b5d1p0"], "quantum_backends": ["ibm_fez"]}
    assert e["verification"]["verdict"] == "contradicted" and len(e["verification"]["findings"]) == 1
    assert e["chain"] == {"position": 2, "of": 2, "prev": _rows(f)[0]["hash"], "hash": _rows(f)[1]["hash"],
                          "content_matches_hash": True}
    assert e["signature"]["state"] == notebook.VALID and e["chain_check"]["ok"] is True
    assert "REAL HARDWARE" in e["text"] and "dbi185ghoj4c73b5d1p0" in e["text"] and "quantum_twin" in e["text"]
    assert "approved by the owner" in e["text"]
    assert notebook.export("dbi185ghoj4c73b5d1p0", f)["seq"] == 2          # found by its IBM job id
    assert notebook.export(_rows(f)[0]["hash"], f)["seq"] == 1             # or by its line hash
    assert len(notebook.export((1, 2), f)["entries"]) == 2


def test_export_never_infers_provenance_or_a_verdict(studio_home, tmp_path):
    """Real-or-null: a row that recorded nothing says so. A row with no verifier verdict is "not recorded", never
    "clean" — the absence of a check is not a passing check."""
    f = _log(tmp_path)
    notebook.record({"tool": "backend_health", "state": "RESOLVED", "args": {}}, f)
    e = notebook.export(1, f)
    assert e["provenance"] == {"recorded": False, "is_real_hardware": None, "ibm_job_ids": [], "quantum_backends": []}
    assert e["verification"] == {"verdict": None, "findings": [], "note": "not recorded on this line"}
    assert "not recorded" in e["text"] and "none (real-or-null: never inferred)" in e["text"]
    assert "clean" not in e["text"]


def test_export_never_shows_a_tampered_row_as_signed(studio_home, tmp_path):
    """The entry is what a due-diligence reader is handed, so it must not render forged parameters under a valid
    signature: the signature covers the line's hash, so the hash is recomputed from the line itself here too. And
    the whole notebook's verdict travels with every entry, including the lines that were not touched."""
    f = _log(tmp_path)
    _three(f)
    rows = _rows(f)
    rows[1]["args"] = {"smiles": "CCO"}
    _write(f, rows)
    bad = notebook.export(2, f)
    assert bad["signature"]["state"] == notebook.BAD and bad["chain"]["content_matches_hash"] is False
    assert "DOES NOT MATCH ITS OWN HASH" in bad["text"] and "NOT VERIFIED" in bad["text"]
    untouched = notebook.export(3, f)                       # its own line is intact; the notebook is not
    assert untouched["chain"]["content_matches_hash"] is True and "NOT VERIFIED" in untouched["text"]


def test_export_reports_a_broken_signature_on_the_run_it_shows(studio_home, tmp_path):
    f = _log(tmp_path)
    notebook.record(ROW, f)
    notebook.sig_path(f).unlink()
    e = notebook.export(1, f)
    assert e["signature"]["state"] == notebook.UNSIGNED and e["chain_check"]["ok"] is False
    assert "none on record" in e["text"]


def test_export_matches_exactly_and_raises_when_there_is_no_such_run(studio_home, tmp_path):
    f = _log(tmp_path)
    notebook.record({**ROW, "detail": "job dbi185ghoj4c73b5d1p0 completed"}, f)
    with pytest.raises(LookupError):
        notebook.export("dbi185ghoj4c73b5d1p0", f)     # it appears in `detail` only: a mention is not a provenance
    with pytest.raises(LookupError):
        notebook.export(99, f)


def test_a_repaired_notebook_verifies_again_and_says_a_repair_happened(tmp_path):
    """A notebook used to be "NOT VERIFIED" for ever after one interrupted write: the quarantined line left an
    orphan signature and the `ledger_repair` line itself went unsigned. A word that can never go back to true
    teaches a reader to ignore it."""
    f = tmp_path / "runs.jsonl"
    notebook.record({"tool": "a"}, f)
    notebook.record({"tool": "b"}, f)
    f.write_bytes(f.read_bytes()[:-14])                 # line 2, already signed, is cut
    notebook.record({"tool": "c"}, f)
    r = notebook.verify(f)
    assert r["ok"] is True and r["why"] is None
    assert r["unsigned"] == 0, "the repair line must be signed too"
    assert r["repairs_on_the_record"] == 1 and r["orphan_signatures"] == 0
    assert "kept, not deleted" in r["notes"]


def test_a_real_removal_still_fails_after_that_change(tmp_path):
    """The note must not become a way to launder a deletion: a removed line breaks the chain, and the repair line
    that would explain one cannot be forged without the key."""
    f = tmp_path / "runs.jsonl"
    for t in "abc":
        notebook.record({"tool": t}, f)
    lines = f.read_text().splitlines()
    f.write_text(lines[0] + "\n" + lines[2] + "\n")
    r = notebook.verify(f)
    assert r["ok"] is False and "removed or re-ordered" in r["why"]


def test_two_processes_cannot_take_the_same_position(tmp_path):
    """`_next_seq` ran under an in-process lock only, so two PROCESSES counted the same seq and `verify` then said
    "line N says it is seq N"."""
    import os
    import subprocess
    import sys
    repo = str(Path(__file__).resolve().parents[1])
    f = tmp_path / "runs.jsonl"
    script = ("import sys; sys.path.insert(0, %r)\n"
              "from pathlib import Path\n"
              "from studio import notebook\n"
              "for i in range(25):\n"
              "    notebook.record({'tool': 'other', 'i': i}, Path(%r))\n" % (repo, str(f)))
    procs = [subprocess.Popen([sys.executable, "-c", script], env={**os.environ, "PYTHONPATH": repo})
             for _ in range(3)]
    for i in range(25):
        notebook.record({"tool": "mine", "i": i}, f)
    for p in procs:
        assert p.wait(timeout=180) == 0
    r = notebook.verify(f)
    assert r["ok"] is True, r
    assert r["lines"] == 100 and r["unsigned"] == 0
