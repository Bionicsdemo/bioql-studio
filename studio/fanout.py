"""Several BioQL calls at once, in one run, when none of them needs another's result.

Until now every call Claude made went down a single stdio pipe, one at a time: ask for a 2D depiction, a 3D
conformer, an ADMET read and a health check — four things that have nothing to say to each other — and BioQL sat
idle for three of the four waits. Worse, a submitted pipeline holds the pipe for as long as it runs, so a 40-minute
job meant Claude could not even ask whether the backend was alive.

What this module owns is the fan-out and its brakes:

- **Only what is independent.** The caller states the calls; this module runs them together and never reorders what
  it gives back. It cannot tell whether call B needs call A's answer, so it does not pretend to: a dependent step is
  a second fan-out, after the first returns.
- **The gate is per call, not per batch.** Each one goes through `Adapter.call` with `by="claude"` and no approval,
  so an entry that needs a person is refused inside the batch exactly as it is alone, and recorded as refused. A
  batch is not a way to get one call past the door by hiding it among nine.
- **One failure does not take the others down.** Every call carries its own state — RESOLVED, ABSENT, UNRESOLVABLE
  or REFUSED — and a raised exception becomes that call's UNRESOLVABLE, never the batch's.
- **Every call keeps its own line on the record**, and all of them carry the same `group`, so the notebook can say
  these ran together. A summary line is written too, counting the four states; it is a summary OF lines that exist,
  not a replacement for them.
- **Bounded.** At most `MAX_CALLS` in one batch and `MAX_WORKERS` at a time, because a fan-out wide enough to make
  BioQL queue is slower than the serial version it replaced and much harder to read.
"""
from __future__ import annotations

import secrets
import time
from concurrent.futures import ThreadPoolExecutor

from .adapter import Adapter, Refused

MAX_CALLS = 16
MAX_WORKERS = 6


def workers_for(n: int, cap: int = MAX_WORKERS) -> int:
    return max(1, min(int(cap), int(n)))


def _one(ad: Adapter, i: int, call: dict, group: str) -> dict:
    name = str((call or {}).get("name") or "")
    args = (call or {}).get("args") or {}
    if not isinstance(args, dict):
        return {"i": i, "name": name, "state": "REFUSED", "detail": "`args` must be an object", "value": None}
    began = time.time()
    try:
        res = ad.call(name, args, approved_by=None, by="claude", group=group)
    except Refused as why:
        # The refusal is already on the record (Adapter.call writes it before raising). Here it becomes this
        # call's state, so the batch reports it instead of dying on it.
        return {"i": i, "name": name, "args": args, "state": "REFUSED", "detail": str(why)[:400],
                "seconds": round(time.time() - began, 3), "value": None}
    except Exception as why:                       # noqa: BLE001 - one call's failure is one call's state
        return {"i": i, "name": name, "args": args, "state": "UNRESOLVABLE",
                "detail": f"{type(why).__name__}: {why}"[:400], "seconds": round(time.time() - began, 3),
                "value": None}
    out = res.as_dict()
    out.update({"i": i, "name": name, "args": args})
    return out


def run_many(ad: Adapter, calls: list, workers: int = MAX_WORKERS, group: str | None = None) -> dict:
    """Run `calls` together. Returns {group, counts, runs} with `runs` in the order given, whatever order they
    finished in: a reader comparing the fourth answer with the fourth question must not have to check.

    `calls` is a list of {name, args}. An empty list is an error, not an empty success — asking for nothing and
    getting "nothing went wrong" is the degradation the three states exist to stop.
    """
    if not isinstance(calls, list) or not calls:
        raise Refused("`calls` must be a non-empty list of {name, args}")
    if len(calls) > MAX_CALLS:
        raise Refused(f"{len(calls)} calls at once; the most this runs together is {MAX_CALLS}. "
                      f"Send them in batches, or run the ones that depend on an answer afterwards.")
    gid = group or secrets.token_hex(6)
    began = time.time()
    pool = ThreadPoolExecutor(max_workers=workers_for(workers), thread_name_prefix=f"fanout-{gid}")
    try:
        runs = list(pool.map(lambda p: _one(ad, p[0], p[1], gid), list(enumerate(calls))))
    finally:
        pool.shutdown(wait=True)
    counts: dict[str, int] = {}
    for r in runs:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    seconds = round(time.time() - began, 3)
    told = ", ".join(f"{n} {s.lower()}" for s, n in sorted(counts.items()))
    ad.record({"tool": "run_many", "state": "GROUP", "group": gid, "by": "claude",
               "detail": f"{len(runs)} call(s) run together in {seconds}s: {told}",
               "calls": [r["name"] for r in runs], "counts": counts, "seconds": seconds})
    return {"group": gid, "seconds": seconds, "counts": counts, "runs": runs,
            "note": "these ran at the same time; each carries its own state, verdict and provenance"}
