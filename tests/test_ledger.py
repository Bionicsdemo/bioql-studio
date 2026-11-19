"""The ledger's six ways of failing to say what happened. Each was reproduced before it was fixed, and each has its
reproduction here so it cannot come back.

The ledger is the one thing that must keep working when everything else is going wrong. A ledger that stops
recording, or that dies when asked whether it is damaged, is worse than no ledger: it reads as "nothing happened".
"""
import json
import os
import threading

import pytest

from studio import ledger


def rows(f):
    return [json.loads(l) for l in f.read_text().splitlines()]


@pytest.fixture
def f(tmp_path):
    return tmp_path / "runs.jsonl"


# ------------------------------------------------------------------ (1) and (2) a torn tail
def test_a_torn_tail_is_reported_not_raised(f):
    ledger.append({"a": 1}, f)
    ledger.append({"a": 2}, f)
    f.write_bytes(f.read_bytes()[:-12])
    r = ledger.verify(f)
    assert r["ok"] is False and r["broken_at"] == 2 and "never finished" in r["why"]
    assert set(r) == {"ok", "lines", "broken_at", "why"}


def test_a_torn_tail_does_not_brick_the_ledger(f):
    """The ledger used to stop recording forever after one interrupted write."""
    ledger.append({"a": 1}, f)
    ledger.append({"a": 2}, f)
    f.write_bytes(f.read_bytes()[:-12])
    out = ledger.append({"a": 3}, f)
    assert out["hash"] and ledger.verify(f)["ok"] is True
    kept = rows(f)
    assert [r.get("tool") for r in kept] == [None, "ledger_repair", None]
    assert kept[-1]["a"] == 3


def test_the_torn_bytes_are_kept_not_deleted(f):
    ledger.append({"a": 1}, f)
    ledger.append({"secret_work": "do not lose me"}, f)
    f.write_bytes(f.read_bytes()[:-12])
    ledger.append({"a": 3}, f)
    moved = ledger.damaged(f)
    assert moved and moved[0]["bytes"] > 0
    kept = (f.parent / moved[0]["file"]).read_text()
    assert "do not lose me" in kept
    rec = [r for r in rows(f) if r.get("tool") == "ledger_repair"][0]
    assert rec["damaged"]["sha256"] and rec["damaged"]["moved_to"] == moved[0]["file"]


def test_a_complete_line_that_does_not_parse_is_moved_aside_too(f):
    ledger.append({"a": 1}, f)
    with f.open("ab") as fh:
        fh.write(b"{not json at all}\n")
    r = ledger.verify(f)
    assert r["ok"] is False and r["broken_at"] == 2
    ledger.append({"a": 3}, f)                  # still records
    assert ledger.verify(f)["ok"] is True
    assert ledger.damaged(f)


# ------------------------------------------------------------------ (3) a line over 64 KB
def test_a_line_longer_than_the_read_window_does_not_brick_it(f):
    """`_last_hash` seeked to size-65536 and parsed a fragment of a longer final line. BioQL returns 96 KB bodies."""
    ledger.append({"x": "y" * 70_000}, f)
    ledger.append({"a": 2}, f)
    assert ledger.verify(f)["ok"] is True and len(rows(f)) == 2


@pytest.mark.parametrize("size", [1, 8191, 8192, 8193, 65_535, 65_537, 300_000])
def test_any_final_line_length_chains_correctly(f, size):
    ledger.append({"x": "y" * size}, f)
    out = ledger.append({"a": 2}, f)
    assert out["prev"] == rows(f)[0]["hash"] and ledger.verify(f)["ok"] is True


# ------------------------------------------------------------------ (4) truncation and the return shape
def test_missing_and_empty_are_reported_as_failures_with_the_same_shape(f):
    r = ledger.verify(f)
    assert set(r) == {"ok", "lines", "broken_at", "why"} and r["ok"] is False and "no ledger" in r["why"]
    f.write_bytes(b"")
    r2 = ledger.verify(f)
    assert r2["ok"] is False and "empty" in r2["why"] and set(r2) == set(r)


def test_a_removed_line_is_caught(f):
    for i in range(3):
        ledger.append({"a": i}, f)
    lines = f.read_text().splitlines()
    f.write_text("\n".join([lines[0], lines[2]]) + "\n")
    r = ledger.verify(f)
    assert r["ok"] is False and r["broken_at"] == 2 and "link" in r["why"]


def test_a_flipped_byte_is_caught(f):
    ledger.append({"a": 1}, f)
    ledger.append({"a": 2}, f)
    raw = f.read_text().replace('"a":2', '"a":3')
    f.write_text(raw)
    r = ledger.verify(f)
    assert r["ok"] is False and r["broken_at"] == 2 and "hash" in r["why"]


# ------------------------------------------------------------------ (5) the parent directory
def test_it_creates_its_own_directory(tmp_path):
    deep = tmp_path / "a" / "b" / "runs.jsonl"
    ledger.append({"a": 1}, deep)
    assert deep.exists() and ledger.verify(deep)["ok"] is True
    assert oct(deep.stat().st_mode)[-3:] == "600"


# ------------------------------------------------------------------ (6) two writers
def test_many_threads_never_fork_the_chain(f):
    def write(i):
        ledger.append({"a": i}, f)

    ts = [threading.Thread(target=write, args=(i,)) for i in range(24)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    r = ledger.verify(f)
    assert r["ok"] is True and r["lines"] == 24


def test_another_process_cannot_fork_the_chain(f, tmp_path):
    """The lock used to be a threading lock, which does nothing between processes: the window and the MCP server
    could read the same `prev`. It is an flock on the ledger now."""
    import subprocess
    import sys
    repo = str(__import__("pathlib").Path(__file__).resolve().parents[1])
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from pathlib import Path\n"
        "from studio import ledger\n"
        "for i in range(40):\n"
        "    ledger.append({'from': 'other process', 'i': i}, Path(%r))\n" % (repo, str(f))
    )
    procs = [subprocess.Popen([sys.executable, "-c", script], env={**os.environ, "PYTHONPATH": repo})
             for _ in range(3)]
    for i in range(40):
        ledger.append({"from": "this process", "i": i}, f)
    for p in procs:
        assert p.wait(timeout=120) == 0
    r = ledger.verify(f)
    assert r["ok"] is True, r
    assert r["lines"] == 160


def test_a_good_ledger_still_verifies(f):
    for i in range(5):
        ledger.append({"tool": "t", "i": i}, f)
    r = ledger.verify(f)
    assert r == {"ok": True, "lines": 5, "broken_at": None, "why": None}


# ------------------------------------------------------------------ the chain's own fields are not the caller's
def test_a_field_called_hash_does_not_break_the_chain_for_ever(f):
    """`hash` is an ordinary word in a scientific payload. It used to be hashed WITH the body while `verify` popped
    it first, so one such line broke the chain at that point permanently."""
    ledger.append({"tool": "t"}, f)
    ledger.append({"tool": "t", "hash": "caller", "prev": "x", "t": 99}, f)
    assert ledger.verify(f)["ok"] is True
    row = rows(f)[-1]
    assert row["row_hash"] == "caller" and row["row_prev"] == "x" and row["row_t"] == 99
    assert row["hash"] != "caller" and row["prev"] == rows(f)[0]["hash"]


def test_two_repairs_in_one_call_are_both_recorded(f):
    """A bad complete line AND an unfinished tail need two quarantines. Only the outer one used to be recorded, so
    the record stopped accounting for what had been set aside."""
    import hashlib
    ledger.append({"a": 1}, f)
    with f.open("ab") as fh:
        fh.write(b"{not json}\n")
    with f.open("ab") as fh:
        fh.write(b'{"half": ')
    ledger.append({"a": 2}, f)
    reps = [r for r in rows(f) if r.get("tool") == "ledger_repair"]
    assert len(reps) == 2 and len(ledger.damaged(f)) == 2
    assert ledger.verify(f)["ok"] is True
    for r in reps:                                   # and each digest proves the file it names
        d = r["damaged"]
        assert hashlib.sha256((f.parent / d["moved_to"]).read_bytes()).hexdigest() == d["sha256"]


def test_two_repairs_in_the_same_second_do_not_share_a_file(f):
    """They did, under one `.damaged-<second>` name opened O_APPEND — and then the sha256 each record carried
    matched neither the file nor the other. The bytes were kept; the digest that proved what was kept was wrong."""
    for i in range(3):
        ledger.append({"a": i}, f)
        with f.open("ab") as fh:
            fh.write(b'{"torn": ')
        ledger.append({"b": i}, f)
    moved = ledger.damaged(f)
    assert len({m["file"] for m in moved}) == len(moved) == 3
