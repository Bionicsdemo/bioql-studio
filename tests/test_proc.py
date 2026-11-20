"""The one comparison that makes BioQL's manifest answer a question: did the process start before or after the edit.

The point of these is the THIRD state. A pid that is gone and a `ps` that could not be asked must not arrive at the
same place, because "the process is not there" is a fact about the process and "I could not look" is not.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from studio import proc


def test_this_very_process_has_a_start_time_and_it_is_in_the_past():
    r = proc.started_at(os.getpid())
    assert r.resolved, r.detail
    # A start time in the future, or before this machine booted, would mean the parse silently went wrong.
    assert r.value <= time.time() + 1
    assert time.time() - r.value < 86400 * 365


def test_a_pid_that_is_not_there_is_absent_and_not_an_error():
    # Walk up from a very high pid until one is free. A pid that exists would make this test assert the wrong thing.
    pid = 4_194_303
    while pid > 100_000:
        if subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode != 0:
            break
        pid -= 1
    r = proc.started_at(pid)
    assert r.absent, f"pid {pid} came back {r.state}: {r.detail}"
    assert "no process" in r.detail


def test_ps_that_cannot_be_asked_is_unresolvable_and_never_absent(monkeypatch):
    def boom(*a, **k):
        raise OSError("ps is not on this machine")
    monkeypatch.setattr(subprocess, "run", boom)
    r = proc.started_at(os.getpid())
    assert r.unresolvable
    assert not r.absent, "a ps that could not be asked must not read as 'the process is gone'"


def test_a_start_time_that_does_not_parse_is_unresolvable_not_a_guess(monkeypatch):
    class Out:
        returncode = 0
        stdout = "martes 20 de noviembre a las dos"
        stderr = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Out())
    r = proc.started_at(os.getpid())
    assert r.unresolvable
    assert "did not parse" in r.detail


def test_ps_is_asked_in_the_c_locale_so_the_month_name_is_not_the_machines(monkeypatch):
    seen = {}

    class Out:
        returncode = 0
        stdout = "Fri Nov 20 13:46:15 2026"
        stderr = ""

    def spy(cmd, **k):
        seen["env"] = k.get("env") or {}
        return Out()
    monkeypatch.setattr(subprocess, "run", spy)
    r = proc.started_at(1)
    assert r.resolved, r.detail
    assert seen["env"].get("LC_ALL") == "C", "without LC_ALL=C the parse depends on the machine's language"


def test_a_file_that_is_not_there_is_absent(tmp_path: Path):
    r = proc.age_of(str(tmp_path / "nunca-existio.py"))
    assert r.absent


def test_a_file_written_before_the_process_started_reads_as_loaded(tmp_path: Path):
    p = tmp_path / "m.py"
    p.write_text("x = 1\n")
    os.utime(p, (time.time() - 3600, time.time() - 3600))
    out = proc.loaded_before_edit(os.getpid(), str(p))
    assert out["verdict"] == "loaded"
    assert "this is what it loaded" in out["says"]


def test_a_file_written_after_the_process_started_reads_as_on_disk_only(tmp_path: Path):
    p = tmp_path / "m.py"
    p.write_text("x = 1\n")
    future = time.time() + 600
    os.utime(p, (future, future))
    out = proc.loaded_before_edit(os.getpid(), str(p))
    assert out["verdict"] == "on_disk_only"
    assert "has not read it" in out["says"]


def test_a_missing_fact_leaves_no_verdict_rather_than_a_no(tmp_path: Path):
    """The degradation this whole module exists to refuse: an unanswerable question answered anyway."""
    out = proc.loaded_before_edit(os.getpid(), str(tmp_path / "no-esta.py"))
    assert out["verdict"] is None, "a file that is not there must not produce a verdict about what loaded"
    assert "cannot be told from here" in out["says"]


@pytest.mark.parametrize("which", ["process", "file"])
def test_either_fact_missing_is_enough_to_withhold_the_verdict(monkeypatch, tmp_path: Path, which):
    p = tmp_path / "m.py"
    p.write_text("x = 1\n")
    if which == "process":
        monkeypatch.setattr(proc, "started_at", lambda pid: proc.unresolvable("ps", "no se pudo preguntar"))
    else:
        monkeypatch.setattr(proc, "age_of", lambda path: proc.unresolvable("stat", "no se pudo leer"))
    out = proc.loaded_before_edit(os.getpid(), str(p))
    assert out["verdict"] is None
    assert out[which]["state"] == "UNRESOLVABLE"
