"""Run the claim-consistency checker over every result BioQL has already produced, and over its live routes.

Read-only, and it executes no pipeline: the corpus is the payloads BioQL itself wrote to disk. That is the only way
to cover hundreds of routes without spending a QPU second or a Modal dollar — and it is better evidence than a
synthetic call, because these are the real answers the system gave.

    python3 tools/sweep_claims.py [--dir PATH] [--limit N] [--json OUT]

Sources swept, in order:
  1. BioQL's artifact store (`~/.bioql/artifacts/*/result.json` and any other json beside it)
  2. a directory given with --dir

Nothing is written into BioQL. The only output is the report.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio import claims                                  # noqa: E402

ARTIFACTS = Path.home() / ".bioql" / "artifacts"
MAX_BYTES = 24 * 1024 * 1024


def payloads(root: Path, limit: int | None = None):
    """(label, payload) for every JSON result under root. A file that will not parse is reported, never skipped in
    silence: 'could not read it' is not 'it is fine'."""
    n = 0
    for p in sorted(root.rglob("*.json")):
        if limit and n >= limit:
            return
        try:
            if p.stat().st_size > MAX_BYTES:
                yield f"{p.parent.name}/{p.name}", {"__unreadable__": f"{p.stat().st_size} bytes, over the limit"}
                n += 1
                continue
            body = json.loads(p.read_text(encoding="utf-8-sig", errors="replace").replace("\0", ""))
        except (OSError, ValueError) as why:
            yield f"{p.parent.name}/{p.name}", {"__unreadable__": f"{type(why).__name__}: {why}"[:160]}
            n += 1
            continue
        yield f"{p.parent.name}/{p.name}", body
        n += 1


def pipeline_of(payload) -> str:
    if isinstance(payload, dict):
        for k in ("pipeline", "pipeline_name", "tool", "endpoint", "url", "mode"):
            v = payload.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()[:48]
    return "?"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, default=ARTIFACTS)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--json", type=Path, help="write the full report here")
    ns = ap.parse_args()

    if not ns.dir.is_dir():
        print(f"no corpus at {ns.dir}")
        return 2

    rows, unreadable = [], []
    codes, by_pipeline, by_field = Counter(), Counter(), Counter()
    for label, body in payloads(ns.dir, ns.limit):
        if isinstance(body, dict) and "__unreadable__" in body:
            unreadable.append((label, body["__unreadable__"]))
            continue
        f = claims.check_claims(body, label)
        rec = claims.execution_record(body)
        v = claims.verdict(f)
        rows.append({"payload": label, "pipeline": pipeline_of(body), "verdict": v,
                     "ran": rec["ran"], "findings": f})
        for x in f:
            codes[x["code"]] += 1
            by_field[x["field"].rsplit(".", 1)[-1]] += 1
        if v == "contradicted":
            by_pipeline[pipeline_of(body)] += 1

    bad = [r for r in rows if r["verdict"] == "contradicted"]
    unver = [r for r in rows if r["verdict"] == "unverifiable"]

    print(f"swept {len(rows)} payloads from {ns.dir}"
          + (f" · {len(unreadable)} unreadable" if unreadable else ""))
    print(f"  contradicted {len(bad)} · unverifiable {len(unver)} · consistent "
          f"{len(rows) - len(bad) - len(unver)}")
    print(f"  execution record: ran {sum(1 for r in rows if r['ran'] is True)} · "
          f"denied {sum(1 for r in rows if r['ran'] is False)} · "
          f"no record {sum(1 for r in rows if r['ran'] is None)}\n")

    if bad:
        print("CONTRADICTED — a field claims quantum execution the payload's own record denies:")
        for pipe, n in by_pipeline.most_common():
            print(f"  {n:>4}  {pipe}")
        print("\n  the fields doing it:")
        for field, n in by_field.most_common(14):
            print(f"  {n:>4}  {field}")
        print("\n  worst examples:")
        for r in sorted(bad, key=lambda x: -len(x["findings"]))[:6]:
            print(f"  · {r['payload']} ({r['pipeline']}) — {len(r['findings'])} findings")
            for x in r["findings"][:3]:
                print(f"      [{x['severity']}] {x['field']}: {x['detail'][:120]}")

    if unreadable:
        print(f"\nunreadable ({len(unreadable)}), reported rather than skipped:")
        for label, why in unreadable[:6]:
            print(f"  · {label}: {why}")

    if ns.json:
        ns.json.write_text(json.dumps({"swept": len(rows), "rows": rows, "unreadable": unreadable}, indent=1,
                                       default=str), encoding="utf-8")
        print(f"\nfull report: {ns.json}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
