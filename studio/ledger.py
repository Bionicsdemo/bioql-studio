"""The run ledger: one line per call to BioQL, chained by hashes (each line carries the previous line's hash).

`studio/notebook.py` signs each line with Ed25519 on top of this; the chain here is what the signature protects.
A line records what was asked, what came back (its sha256, not the payload), the state, the verifier's verdict and
the provenance BioQL itself reported — real-or-null, never inferred.

The ledger is the one thing in Studio that must keep working when everything else is going wrong, because its job is
to say what happened. Six ways it stopped doing that, all found by review and each reproduced before it was fixed:

1. A torn last line — a write cut short by a crash or a full disk — made `verify` raise instead of reporting it.
2. The same torn line bricked `append` FOREVER: every later call raised, so the ledger quietly stopped recording.
3. One line over 64 KB bricked `append` the same way, because `_last_hash` seeked to `size - 65536` and parsed the
   last line in that window, which for a longer final line is a fragment. Not hypothetical with 96 KB result bodies
   in play: the rows hold a sha256, but `args` and `detail` are unbounded.
4. A missing or emptied file answered `{"ok": True, "lines": 0}` — no `broken_at`, no `why`, a different shape from
   every failure path, and truncation reading as success.
5. `append` did not create its parent directory.
6. The lock was per-process, so the MCP server and the window could read the same `prev` and fork the chain.

What replaces them: the tail is found by scanning backwards for a real line boundary, with no size assumption; a
tail that was never finished is quarantined into `<name>.damaged-<ts>` and a `ledger_repair` line records its digest,
so recording continues and nothing is thrown away; `verify` always returns the same four keys and reports damage
instead of raising; and the lock is an `flock` on the file itself, which holds across processes.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import threading
import time
from pathlib import Path

from . import home

try:
    import fcntl
except ImportError:                       # not POSIX: the in-process lock is all there is, and verify() says so
    fcntl = None                          # type: ignore[assignment]

_LOCK = threading.RLock()
READ_BACK = 8192                          # the first window to look back for a line boundary; it grows as needed


def canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def digest(obj) -> str:
    return hashlib.sha256(obj if isinstance(obj, bytes) else canon(obj)).hexdigest()


def path_file() -> Path:
    return home() / "runs.jsonl"


def _tail_line(f: Path) -> tuple[bytes, bool]:
    """(the last line's bytes, whether the file ends with a newline).

    Scans backwards in growing windows for a real line boundary, so a last line of any length is read whole. The
    fixed 64 KB window this replaces turned a long final line into a fragment and bricked every later append.
    """
    size = f.stat().st_size
    if size == 0:
        return b"", True
    with f.open("rb") as fh:
        back = READ_BACK
        while True:
            start = max(0, size - back)
            fh.seek(start)
            chunk = fh.read(size - start)
            ended = chunk.endswith(b"\n")
            body = chunk[:-1] if ended else chunk
            i = body.rfind(b"\n")
            if i != -1:
                return body[i + 1:], ended
            if start == 0:
                return body, ended        # the whole file is one line
            back *= 4                     # no boundary in this window: look further back


def _quarantine(f: Path, keep: int, why: str) -> dict:
    """Move the unfinished tail aside and record what was moved. Nothing is deleted: the bytes go to a `.damaged`
    file next to the ledger, and the next line names their digest, so a reader can see that something was there."""
    with f.open("rb") as fh:
        fh.seek(keep)
        bad = fh.read()
    # A name per repair, not per second: two repairs in the same second shared one file, and then the
    # sha256 each record carried matched neither the file nor the other. The bytes were kept and the
    # digest that was supposed to prove WHAT was kept was wrong.
    base = f"{f.name}.damaged-{int(time.time())}"
    for n in range(1000):
        hurt = f.with_name(base if n == 0 else f"{base}-{n}")
        try:
            fd = os.open(hurt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            continue
    else:
        raise RuntimeError("too many damaged tails in one second")
    with os.fdopen(fd, "ab") as out:
        out.write(bad)
    with f.open("r+b") as fh:
        fh.truncate(keep)
    return {"bytes": len(bad), "sha256": hashlib.sha256(bad).hexdigest(), "moved_to": hurt.name, "why": why}


def _state(f: Path, found: list | None = None) -> tuple[str, list]:
    """(the hash to chain onto, EVERY repair this took).

    The list matters: a file with a bad complete line AND an unfinished tail needs two quarantines, and an
    earlier version returned only the outer one — both tails were moved aside and only one was recorded, so the
    record no longer accounted for what had been set aside.
    """
    found = [] if found is None else found
    if not f.exists() or f.stat().st_size == 0:
        return "0" * 64, found
    line, ended = _tail_line(f)
    try:
        row = json.loads(line)
        if not isinstance(row, dict) or not isinstance(row.get("hash"), str):
            raise ValueError("a ledger line must be an object carrying its hash")
        if ended:
            return row["hash"], found
    except ValueError as why:
        row, _ = None, None
        if ended:
            # A complete line that does not parse is corruption, not an interrupted write. It is moved aside too —
            # refusing instead would brick the ledger, which is the defect this whole module exists to avoid — and
            # the repair line says what it was.
            keep = f.stat().st_size - len(line) - 1
            found.append(_quarantine(f, max(0, keep), f"the last line did not parse: {why}"[:160]))
            return _state(f, found)
    # the tail is not newline-terminated: a write that never finished
    keep = f.stat().st_size - len(line)
    found.append(_quarantine(f, keep, "the last line was never finished (no newline): a write cut short"))
    return _state(f, found)


RESERVED = ("hash", "prev", "t")


def _write(f: Path, row: dict) -> dict:
    # The chain's own fields are the ledger's, never the caller's. A row carrying a field literally named `hash`
    # was hashed WITH it while `verify` pops `hash` before hashing, so one such line broke the chain at that point
    # for ever — and `hash` is an ordinary word in a scientific payload. What the caller sent is kept, under
    # `row_<name>`, so nothing is lost and nothing is silently overwritten either.
    body = {}
    for k, v in row.items():
        body[f"row_{k}" if k in RESERVED else k] = v
    body["t"] = round(time.time(), 3)
    body["prev"] = _PREV[0]
    body["hash"] = digest(body)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab") as fh:
        fh.write(canon(body) + b"\n")
    _PREV[0] = body["hash"]
    return body


_PREV = [""]                              # the hash the next line chains onto, held only inside a locked section


def append(row: dict, file: Path | None = None) -> dict:
    """Add one line. Takes an flock on the ledger itself, so two PROCESSES — the window and the MCP server — cannot
    read the same `prev` and fork the chain."""
    f = file or path_file()
    f.parent.mkdir(parents=True, exist_ok=True)      # (5) a ledger that cannot be created records nothing
    with _LOCK:
        lock = os.open(f, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                except OSError as why:               # a filesystem without locks: the in-process lock still holds
                    if why.errno not in (errno.ENOLCK, errno.EOPNOTSUPP, errno.EINVAL):
                        raise
            prev, repairs = _state(f)
            _PREV[0] = prev
            for repair in repairs:
                _write(f, {"tool": "ledger_repair", "state": "REPAIRED", "detail": repair["why"],
                           "damaged": repair, "by": "ledger"})
            return _write(f, row)
        finally:
            os.close(lock)


def verify(file: Path | None = None) -> dict:
    """{ok, lines, broken_at, why} — always those four keys, whatever happened.

    Damage is REPORTED, never raised: a verifier that dies on a damaged file cannot tell you the file is damaged.
    """
    f = file or path_file()
    out = {"ok": True, "lines": 0, "broken_at": None, "why": None}
    if not f.exists():
        return {**out, "ok": False, "why": f"no ledger at {f}"}
    try:
        raw = f.read_bytes()
    except OSError as why:
        return {**out, "ok": False, "why": f"cannot read it: {type(why).__name__}: {why}"[:200]}
    if not raw:
        return {**out, "ok": False, "why": "the ledger is empty: nothing recorded, or it was truncated"}
    prev, n = "0" * 64, 0
    lines = raw.split(b"\n")
    ended = raw.endswith(b"\n")
    if ended:
        lines = lines[:-1]
    for n, line in enumerate(lines, 1):
        last = n == len(lines)
        try:
            rowdict = json.loads(line)
            if not isinstance(rowdict, dict):
                raise ValueError("not an object")
        except ValueError as why:
            tail = " (the last line was never finished: a write cut short)" if last and not ended else ""
            return {**out, "ok": False, "lines": n, "broken_at": n,
                    "why": f"line {n} is not valid JSON: {why}{tail}"[:200]}
        h = rowdict.pop("hash", None)
        if rowdict.get("prev") != prev:
            return {**out, "ok": False, "lines": n, "broken_at": n,
                    "why": f"line {n} does not link to the one before it"}
        if digest(rowdict) != h:
            return {**out, "ok": False, "lines": n, "broken_at": n,
                    "why": f"line {n} does not match its own hash"}
        prev = h
    return {**out, "lines": n}


def damaged(file: Path | None = None) -> list[dict]:
    """The tails moved aside by a repair, newest first. They are kept, not deleted."""
    f = file or path_file()
    out = []
    for p in sorted(f.parent.glob(f"{f.name}.damaged-*"), reverse=True):
        try:
            out.append({"file": p.name, "bytes": p.stat().st_size, "at": p.name.rsplit("-", 1)[-1]})
        except OSError:
            continue
    return out
