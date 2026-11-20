"""When a process started — the one fact that turns BioQL's manifest from a list into an answer.

BioQL's `runtime_manifest` hashes each module's file **at the moment it is asked** (`sha256_of` stats the path), not
the bytes the interpreter imported. So a module edited after the backend started reports its NEW hash while the
process keeps executing the OLD code. The manifest alone therefore cannot answer the question anybody actually has,
which is: *is the fix I just made live, or is it only on disk?*

One comparison answers it: the process's start time against the file's mtime. Edited BEFORE the process started →
that is what it loaded. Edited AFTER → it is on disk and the process has not read it.

Three states, kept apart, because this is a fact about something outside this program:
  RESOLVED      `ps` answered with a start time.
  ABSENT        `ps` answered and there is no such pid — the process is gone. That IS information.
  UNRESOLVABLE  `ps` could not be asked or did not parse. That is NOT information about the process.

`ps` and not /proc: this is macOS, where /proc does not exist. `-o lstart=` prints a local-time string without a
zone, so it is parsed as local time, which is what it is.
"""
from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime

from .resolution import Resolution, absent, resolved, unresolvable

TIMEOUT = 5.0

# `ps -o lstart=` on macOS prints e.g. "vie. 20 nov 13:46:15 2026" — the weekday and month are LOCALISED, so
# parsing that text means depending on the machine's locale. `-o lstart=` is asked with LC_ALL=C to force the
# C-locale spelling ("Fri Nov 20 13:46:15 2026"), which is stable. A locale that still surprises us comes back
# UNRESOLVABLE, never as a guess.
FORMAT = "%a %b %d %H:%M:%S %Y"


def started_at(pid: int) -> Resolution:
    """When the process with this pid started, as a unix timestamp."""
    try:
        p = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))],
                           capture_output=True, text=True, timeout=TIMEOUT,
                           env={**os.environ, "LC_ALL": "C"})
    except (OSError, ValueError, subprocess.SubprocessError) as why:
        return unresolvable("ps", f"could not ask ps about pid {pid}: {type(why).__name__}: {str(why)[:120]}")
    raw = (p.stdout or "").strip()
    if p.returncode != 0 or not raw:
        # `ps` exits non-zero for a pid that is not there. The process being gone is an answer about the process.
        return absent("ps", f"no process with pid {pid} on this machine")
    try:
        when = datetime.strptime(" ".join(raw.split()), FORMAT)
    except ValueError:
        return unresolvable("ps", f"ps answered for pid {pid} but the start time did not parse: {raw[:80]!r}")
    return resolved(when.timestamp(), "ps", f"pid {pid} started {when.isoformat(timespec='seconds')}")


def age_of(path: str) -> Resolution:
    """When a file was last written, as a unix timestamp. A path that is not there is ABSENT, not an error."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return absent("stat", f"{path} is not there")
    except OSError as why:
        return unresolvable("stat", f"could not stat {path}: {type(why).__name__}: {str(why)[:120]}")
    return resolved(st.st_mtime, "stat", f"{path} last written {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime))}")


def loaded_before_edit(pid: int, path: str) -> dict:
    """Did the process start AFTER this file was last written?

    Returns the two facts and a verdict over them, each of the three states kept. The verdict is `None` whenever
    either fact is missing — the question has no answer then, and a missing fact never becomes a "yes" or a "no".
    """
    proc = started_at(pid)
    file = age_of(path)
    out: dict = {"pid": pid, "path": path,
                 "process": {"state": proc.state, "detail": proc.detail},
                 "file": {"state": file.state, "detail": file.detail},
                 "verdict": None, "says": None}
    if not (proc.resolved and file.resolved):
        out["says"] = ("cannot be told from here: " +
                       "; ".join(r.detail for r in (proc, file) if not r.resolved))
        return out
    started, written = proc.value, file.value
    out["process"]["at"] = started
    out["file"]["at"] = written
    if written <= started:
        out["verdict"] = "loaded"
        out["says"] = "this file was already written when the process started, so this is what it loaded"
    else:
        out["verdict"] = "on_disk_only"
        out["says"] = (f"this file was written {int(written - started)}s AFTER the process started: it is on disk "
                       "and this process has not read it")
    return out
