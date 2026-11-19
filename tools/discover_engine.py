"""Every route BioQL's ENGINE declares, not just the ones its window happens to call.

The first sweep read the frontend and found 181 paths. That was the wrong question: the frontend is one consumer of
the engine, and the engine declares 514 routes across 99 files. A tool nobody has wired into the window yet is
still a tool, and the new system is supposed to reach ALL of them.

So this reads the route declarations out of BioQL's own backend source — every `@app.route("…")` and its blueprint
cousins — and then proves each one live WITHOUT running it: a GET on a POST-only Flask route answers 405 with an
`Allow` header naming its methods. No pipeline body is ever sent.

One file cannot be read: `script_executor_server.py` is in the owner's own deny list, and it holds 249 of the 514.
Those are covered the other way round — from the window, from BioQL's MCP server and from its live registry, which
is what the first two sweeps did. This sweep adds the 265 the monolith does not hold, and says plainly how many it
could not see.

    python3 tools/discover_engine.py            report
    python3 tools/discover_engine.py --write    add the live ones to Studio's allowlist
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
from tools.discover_surface import area_of, entry_for          # noqa: E402

BACKEND = Path.home() / "Documents/Proyectos/Spectrix_BioQL/src/backend"
DENIED = {"script_executor_server.py", "replicon_safety.py", "bioethics_safety_filter.py", "mission_gate.py"}
ROUTE = re.compile(r"""@\w*(?:app|bp|blueprint|api)\w*\.route\(\s*["']([^"']+)["']([^)]*)\)""", re.I)
METHODS = re.compile(r"""["'](GET|POST|PUT|DELETE|PATCH)["']""")
# A Flask converter segment, `<int:job_id>` or `<name>`, becomes a named parameter.
SEG = re.compile(r"<(?:[a-z]+:)?(\w+)>")


def declared() -> dict[str, dict]:
    """path -> {methods, files}. Read-only; BioQL's source is never modified and the denied files are never opened."""
    out: dict[str, dict] = {}
    if not BACKEND.is_dir():
        return out
    for f in sorted(BACKEND.glob("*.py")):
        if f.name in DENIED:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in ROUTE.finditer(text):
            raw, tail = m.group(1), m.group(2)
            if not raw.startswith("/"):
                continue
            path = SEG.sub(lambda s: "{" + s.group(1) + "}", raw).rstrip("/")
            e = out.setdefault(path, {"methods": set(), "files": set()})
            e["methods"].update(METHODS.findall(tail) or ["GET"])
            e["files"].add(f.name)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--limit", type=int)
    ns = ap.parse_args()
    base = bioql_url()
    routes = declared()
    if not routes:
        print(f"no backend source at {BACKEND}")
        return 2
    print(f"{len(routes)} routes declared across BioQL's readable backend files")
    print(f"(the monolith {sorted(DENIED)[3]} is in the owner's deny list and holds ~249 more; those are covered "
          f"by the window and registry sweeps)\n")

    rows, cache = [], {}
    for path in sorted(routes)[: ns.limit]:
        probe_path = re.sub(r"\{(\w+)\}", "studio-probe", path)
        if probe_path not in cache:
            cache[probe_path] = probe(probe_path, base)
        rows.append({"path": path, **routes[path], **cache[probe_path], "area": area_of(path)})

    live = [r for r in rows if r.get("exists") is True]
    known = {e["path"] for e in al.load().values()}
    fresh = [r for r in live if r["path"] not in known and not al.DENY.search(r["path"])
             and not al.MODELS.search(r["path"])]

    for r in sorted(rows, key=lambda x: (x["area"], x["path"])):
        mark = {True: "live", False: "not mounted", None: "could not ask"}[r.get("exists")]
        new = " NEW" if any(x["path"] == r["path"] for x in fresh) else ""
        print(f"{r['area']:<10} {r['path']:<56} {mark}{new}")

    print(f"\nlive {len(live)} · not mounted {len(rows) - len(live)} · new to Studio {len(fresh)}")
    print("new by area: " + " · ".join(f"{a} {n}" for a, n in Counter(r["area"] for r in fresh).most_common()))

    if not ns.write:
        print("\n(nothing written; pass --write to add the new live ones)")
        return 0

    f = al.path_file()
    raw = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    added, skipped = 0, []
    for r in fresh:
        name = re.sub(r"[^a-z0-9]+", "_", r["path"][1:].lower()).strip("_")[:60]
        # Never overwrite a built-in: those were written by hand and this only guesses from a route.
        if not name or name in raw or name in al.DEFAULTS:
            continue
        e = entry_for(r["path"], r["methods"], r["http"], r.get("allow"))
        e["why"] = f"[{r['area']}] declared by BioQL in {sorted(r['files'])[0]}"
        e["from_registry"] = {**e["from_registry"], "route_from": "bioql-backend",
                              "declared_in": sorted(r["files"])[:3]}
        bad = al.problems(al._norm(name, e))
        if bad:
            skipped.append(f"{name} ({'; '.join(bad)})")
            continue
        raw[name] = e
        added += 1
    write_private(f, json.dumps(raw, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"\nadded {added} entries ({len(raw)} in the allowlist now)")
    if skipped:
        print(f"skipped {len(skipped)}: " + ", ".join(skipped[:10]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
