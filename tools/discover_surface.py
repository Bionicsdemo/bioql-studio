"""Everything BioQL's own window calls, verified live, so Studio's new interface can reach ALL of it.

The window on :3000 is the whole product: chat, tools, results, 3D, targets, lab notebook, screening, SAR, report
builder, drug candidates, server health, the budget. Studio is a NEW interface over the same engine — so it needs
the same reach, not a sample of it.

This reads the paths BioQL's own frontend uses, groups them, and proves each one exists WITHOUT running it: a GET on
a POST-only Flask route answers 405 with an `Allow` header. No pipeline body is ever sent, so nothing can start a
run, spend a QPU second or touch Modal.

    python3 tools/discover_surface.py            report
    python3 tools/discover_surface.py --write    add the live ones to Studio's allowlist

Nothing is written into BioQL. Reading its frontend source is reading; the engine is only ever asked whether a route
is there.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio import allowlist as al, bioql_url, write_private   # noqa: E402
from tools.discover_routes import probe                        # noqa: E402

UI = Path.home() / "Documents/Proyectos/Spectrix_BioQL/src/frontend/src"
CALL = re.compile(r"""['"`](/api/[A-Za-z0-9_/.$%{}-]*)['"`]""")
METHOD = re.compile(r"method:\s*['\"](GET|POST|PUT|DELETE|PATCH)['\"]", re.I)

# What a path is FOR, so the new interface can lay it out the way a person works rather than as a flat list. The
# first pattern that matches wins, so the order is the grouping.
AREAS = [
    ("health", r"^/api/(health|system/|v1/quantum/health|quantum/health|protenix/health|targetradar/health)"),
    ("jobs", r"^/api/(pipeline/job|pipeline/submit|recovery/|scripts/(status|output))"),
    ("chem", r"^/api/(smiles-to-3d|render/|rdkit|motifs/|chem/|structure/|viz/)"),
    ("targets", r"^/api/(targets|targetradar/|motifs)"),
    ("screening", r"^/api/(screening/|docking/|vina|sar/)"),
    ("notebook", r"^/api/(eln/|reports?/|candidates)"),
    ("quantum", r"^/api/(quantum/|v11/|complexa/|protenix/)"),
    ("pipeline", r"^/api/(pipeline/|casestudy/|aging/|bionics/|genomics/)"),
    ("tools", r"^/api/(tools/|bioql|validation/|typesafe/)"),
]


def area_of(path: str) -> str:
    for name, pat in AREAS:
        if re.match(pat, path):
            return name
    return "other"


def ui_paths() -> dict[str, set[str]]:
    """path -> the methods the frontend uses with it. A `${...}` segment becomes a named parameter."""
    found: dict[str, set[str]] = {}
    if not UI.is_dir():
        return found
    for f in list(UI.rglob("*.ts")) + list(UI.rglob("*.tsx")):
        if ".bak" in f.name or ".test." in f.name or "__fixtures__" in str(f):
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        for m in CALL.finditer(text):
            raw = m.group(1)
            near = text[m.end():m.end() + 260]
            meth = (METHOD.search(near) or [None, "GET"])[1].upper() if METHOD.search(near) else "GET"
            path = re.sub(r"\$\{[^}]*\}", "{id}", raw).rstrip("/")
            if not path.startswith("/api/") or len(path) < 6:
                continue
            found.setdefault(path, set()).add(meth)
    return found


def entry_for(path: str, methods: set[str], http: int, allow: str | None) -> dict:
    """An allowlist entry. The METHOD comes from what the route itself says it accepts (its Allow header), not from
    a guess: a route that only takes POST is not reachable with a GET however the frontend writes it."""
    takes = {m.strip().upper() for m in (allow or "").split(",") if m.strip()} or methods
    method = "POST" if "POST" in takes and "GET" not in takes else ("GET" if "GET" in takes else "POST")
    params = re.findall(r"\{(\w+)\}", path)
    area = area_of(path)
    # Only a read is cheap. Everything that computes asks a person, because BioQL's registry never says whether a
    # route reaches a QPU (its cost tiers are only high and medium).
    kind = "status" if (method == "GET" and area in ("health", "jobs")) else (
        "local" if area == "chem" and method == "POST" else ("status" if method == "GET" else "compute"))
    return {"method": method, "path": path, "via": "direct" if kind != "compute" else "submit", "kind": kind,
            "required": params, "optional": [], "timeout": 43200 if kind == "compute" else 300,
            "absent_on_404": kind == "status", "why": f"[{area}] used by BioQL's own window",
            "from_registry": {"route_from": "bioql-ui", "verified_http": http, "area": area,
                              "ui_methods": sorted(methods), "route_confirmed_by_bioql": True}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ns = ap.parse_args()
    base = bioql_url()
    paths = ui_paths()
    if not paths:
        print(f"no frontend source at {UI}")
        return 2
    print(f"{len(paths)} distinct /api paths in BioQL's own window\n")

    rows, cache = [], {}
    for path in sorted(paths):
        probe_path = re.sub(r"\{(\w+)\}", "studio-probe", path)
        if probe_path not in cache:
            cache[probe_path] = probe(probe_path, base)
        r = cache[probe_path]
        rows.append({"path": path, "methods": paths[path], **r, "area": area_of(path)})

    live = [r for r in rows if r.get("exists") is True]
    gone = [r for r in rows if r.get("exists") is False]
    unknown = [r for r in rows if r.get("exists") is None]
    by_area = Counter(r["area"] for r in live)

    for r in sorted(rows, key=lambda x: (x["area"], x["path"])):
        mark = {True: "live", False: "not on this backend", None: "could not ask"}[r.get("exists")]
        deny = " [never callable]" if al.DENY.search(r["path"]) else ""
        print(f"{r['area']:<10} {r['path']:<52} {mark}{deny}" + (f"  allow={r['allow']}" if r.get("allow") else ""))

    print(f"\nlive {len(live)} · not on this backend {len(gone)} · could not ask {len(unknown)}")
    print("by area: " + " · ".join(f"{a} {n}" for a, n in by_area.most_common()))

    if not ns.write:
        print("\n(nothing written; pass --write to put the live ones in Studio's allowlist)")
        return 0

    f = al.path_file()
    raw = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    added, skipped = 0, []
    for r in live:
        if al.DENY.search(r["path"]):
            skipped.append(r["path"])
            continue
        name = re.sub(r"[^a-z0-9]+", "_", r["path"][len("/api/"):].lower()).strip("_")[:60]
        if not name or name in raw:
            continue
        e = entry_for(r["path"], r["methods"], r["http"], r.get("allow"))
        bad = al.problems(al._norm(name, e))
        if bad:
            skipped.append(f"{name} ({'; '.join(bad)})")
            continue
        raw[name] = e
        added += 1
    write_private(f, json.dumps(raw, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"\nadded {added} entries ({len(raw)} in the allowlist now) → {f}")
    if skipped:
        print(f"skipped {len(skipped)}: " + ", ".join(skipped[:12]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
