"""The run ledger: one line per call to BioQL, chained by hashes (each line carries the previous line's hash).

Phase 1 chains; Phase 2 signs each line with Ed25519 (the lab notebook). A line records what was asked, what came
back (its sha256, not the payload), the state (RESOLVED / ABSENT / UNRESOLVABLE) and the provenance BioQL itself
reported — real-or-null, never inferred.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

from . import home

_LOCK = threading.Lock()


def canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def digest(obj) -> str:
    return hashlib.sha256(obj if isinstance(obj, bytes) else canon(obj)).hexdigest()


def path_file() -> Path:
    return home() / "runs.jsonl"


def _last_hash(f: Path) -> str:
    if not f.exists() or f.stat().st_size == 0:
        return "0" * 64
    with f.open("rb") as fh:
        fh.seek(max(0, f.stat().st_size - 65536))
        tail = fh.read().splitlines()
    return json.loads(tail[-1])["hash"]


def append(row: dict, file: Path | None = None) -> dict:
    f = file or path_file()
    with _LOCK:
        body = {**row, "t": round(time.time(), 3), "prev": _last_hash(f)}
        body["hash"] = digest(body)
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "ab") as fh:
            fh.write(canon(body) + b"\n")
    return body


def verify(file: Path | None = None) -> dict:
    """{ok, lines, broken_at, why}: every line's hash recomputed and every link checked."""
    f = file or path_file()
    prev, n = "0" * 64, 0
    if not f.exists():
        return {"ok": True, "lines": 0}
    for n, line in enumerate(f.read_bytes().splitlines(), 1):
        row = json.loads(line)
        h = row.pop("hash", None)
        if row.get("prev") != prev:
            return {"ok": False, "lines": n, "broken_at": n, "why": "link to the previous line does not match"}
        if digest(row) != h:
            return {"ok": False, "lines": n, "broken_at": n, "why": "line content does not match its hash"}
        prev = h
    return {"ok": True, "lines": n}
