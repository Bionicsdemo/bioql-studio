"""Live end-to-end check against the BioQL backend that is already running (it never starts or stops it).

Wraps a set of cheap, local calls in the no-change guard and prints one line per call plus the guard's verdict.
Nothing here can reach a QPU, Modal or any paid service: only status entries and two local RDKit computations.

    python3 tools/e2e_live.py            exit 0: every call behaved and BioQL did not change
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio import guard, ledger                      # noqa: E402
from studio.adapter import Adapter, Refused           # noqa: E402

PARACETAMOL = "CC(=O)Nc1ccc(O)cc1"
CALLS = [  # (tool, args, expected state)
    ("backend_health", {}, "RESOLVED"),
    ("tools_registry", {}, "RESOLVED"),
    ("live_status", {}, "RESOLVED"),
    ("motifs_list", {}, "RESOLVED"),
    ("smiles_to_3d", {"smiles": PARACETAMOL}, "RESOLVED"),
    ("render_2d", {"smiles": PARACETAMOL, "width": 200, "height": 150, "format": "png"}, "RESOLVED"),
    ("pipeline_job", {"job_id": "studio-e2e-no-such-job"}, None),          # ABSENT if BioQL 404s; reported as is
    ("tools_registry", {"smuggled": 1}, "REFUSED"),
    ("scripts_execute", {}, "REFUSED"),
]


def main() -> int:
    ad = Adapter()
    ok = True
    try:
        with guard.session() as g:
            for tool, args, want in CALLS:
                try:
                    res = ad.call(tool, args, by="e2e")
                    state, detail = res.state, res.detail
                    v = res.value if res.resolved else None
                    shape = list(v)[:5] if isinstance(v, dict) else type(v).__name__
                    prov = res.meta.get("provenance")
                except Refused as why:
                    state, detail, shape, prov = "REFUSED", str(why), "-", None
                good = want is None or state == want
                ok &= good
                print(f"{'ok ' if good else 'BAD'} {tool:<16} {state:<12} {detail[:60]:<60} {shape} {prov or ''}")
    except guard.Changed as ch:
        print("\nGUARD: BioQL changed during the session")
        for name, r in ch.report["roots"].items():
            if not r["clean"]:
                print(f"  {name}: added {r['added'][:10]} removed {r['removed'][:10]} changed {r['changed'][:10]} git {list(r['git'])}")
        return 4
    print(f"\nGUARD: clean — {sum(r['files'] for r in g['report']['roots'].values())} BioQL files unchanged")
    print("LEDGER:", json.dumps(ledger.verify()))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
