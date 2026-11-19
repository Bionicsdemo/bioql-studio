"""Find the real route of every pipeline in BioQL's registry, and verify each route exists — without running one.

Three sources, in order of authority:
  1. BioQL's live registry, /api/tools/registry: 63 pipelines. 26 state their own endpoint.
  2. BioQL's own UI route map, src/frontend/src/services/MCPClient.ts: the routes the other 37 are reached by.
  3. A method probe against the live backend: a GET on a POST-only Flask route answers 405 with an `Allow` header,
     which proves the route EXISTS and names its methods — without executing the pipeline. 404 means it does not
     exist. Nothing here sends a pipeline body, so nothing can start a run, spend a QPU or touch Modal.

Writes the result as a report (and, with --write, as Studio's allowlist file: every entry `compute`, so every entry
asks a person before it runs).

    python3 tools/discover_routes.py [--write]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio import allowlist as al, bioql_url, write_private   # noqa: E402
from studio.adapter import Adapter                             # noqa: E402

BIOQL = Path.home() / "Documents/Proyectos/Spectrix_BioQL"
UI_MAPS = [BIOQL / "src/frontend/src/services/MCPClient.ts", BIOQL / "src/frontend/src/services/VSIXService.ts"]
MCP_SERVER = BIOQL / "src/mcp/server.py"
ENTRY = re.compile(r"^\s*'([a-z0-9_]+)':\s*'(/api/[A-Za-z0-9_/-]+)'", re.M)
URL = re.compile(r"[\"'](?:https?://[\d.:a-z]+)?(/api/[A-Za-z0-9_/-]+)[\"']")
GROUPS = ("proteomic", "ppi", "inverse", "docking", "combination", "mps", "gpu", "manybody")


def ui_routes(paths: list[Path] | None = None) -> dict[str, str]:
    """name -> route, as BioQL's own UI holds it. Read-only; the files are not modified."""
    out: dict[str, str] = {}
    for p in paths or UI_MAPS:
        if p.exists():
            for m in ENTRY.finditer(p.read_text(encoding="utf-8", errors="replace")):
                out.setdefault(m.group(1), m.group(2))
    return out


def mcp_routes(path: Path = MCP_SERVER) -> set[str]:
    """Every /api/... route BioQL's own MCP server posts to. A route, not a name: matched against candidates."""
    if not path.exists():
        return set()
    return set(URL.findall(path.read_text(encoding="utf-8", errors="replace")))


def candidates(name: str, known: set[str]) -> list[str]:
    """Routes a pipeline of this name could live at, in order of likelihood. The nested form is how BioQL's own MCP
    server reaches the proteomic, PPI, inverse-folding and docking families (`quantum_ppi_binding_affinity` is at
    /api/quantum/ppi/binding_affinity, not at the flat name)."""
    out = [f"/api/quantum/{name}", f"/api/pipeline/{name}"]
    bare = name[len("quantum_"):] if name.startswith("quantum_") else name
    if bare != name:
        out += [f"/api/quantum/{bare}", f"/api/pipeline/{bare}"]
    for g in GROUPS:
        if bare.startswith(g + "_"):
            out.insert(0, f"/api/quantum/{g}/{bare[len(g) + 1:]}")
    # A route BioQL's MCP server posts to, but ONLY where its last segment is this pipeline's whole name. Matching a
    # mere shared last word would hand a pipeline someone else's route: an earlier version of this function sent
    # `viral_replicon_design` to /api/quantum/combination/design because both end in "design". A wrong route is worse
    # than no route — it would run the wrong science under the right name. Match whole segments, never substrings.
    out += sorted(r for r in known if r.rstrip("/").rsplit("/", 1)[-1] in (bare, name))
    return list(dict.fromkeys(out))


def probe(route: str, base: str) -> dict:
    """Does this route exist? A GET with no body. 405 => exists (POST-only). 200/4xx => exists. 404 => it does not."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(base + route, method="GET")
    try:
        with opener.open(req, timeout=20) as r:
            return {"exists": True, "http": r.status, "allow": r.headers.get("Allow")}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"exists": False, "http": 404, "allow": None}
        return {"exists": True, "http": e.code, "allow": e.headers.get("Allow")}
    except Exception as e:                                      # noqa: BLE001 - reported, never guessed
        return {"exists": None, "http": None, "allow": None, "why": f"{type(e).__name__}: {e}"[:120]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="write the verified entries into Studio's allowlist")
    ns = ap.parse_args()

    base = bioql_url()
    res = Adapter(log=False).call("tools_registry")
    if not res.resolved:
        print(f"BioQL's registry is {res.state}: {res.detail}")
        return 2
    tools = res.value["tools"]
    ui, mcp = ui_routes(), mcp_routes()
    print(f"{len(tools)} pipelines in BioQL's registry · {len(ui)} routes in its UI maps · "
          f"{len(mcp)} in its MCP server\n")

    rows, cache = [], {}

    def check(route: str) -> dict:
        if route not in cache:
            cache[route] = probe(route, base)
        return cache[route]

    for t in sorted(tools, key=lambda x: x["name"]):
        name = t["name"]
        row = {"name": name, "kind": t.get("kind"), "schema": t.get("schema") or {}}
        tried = []
        for route, where in [(t.get("endpoint"), "registry"), (ui.get(name), "ui-map")]:
            if route:
                tried.append((route, where))
        tried += [(c, "derived") for c in candidates(name, mcp)]
        for route, where in tried:
            r = check(route)
            if r.get("exists") is True:
                rows.append({**row, "route": route, "where": where, **r})
                break
        else:
            first = tried[0] if tried else (None, "none")
            rows.append({**row, "route": first[0], "where": first[1], "exists": False, "http": 404, "allow": None,
                         "tried": [t for t, _ in tried]})

    live = [r for r in rows if r.get("exists") is True]
    gone = [r for r in rows if r.get("exists") is False]
    none = [r for r in rows if r.get("exists") is None]
    for r in rows:
        mark = {True: "live", False: "NOT ON BACKEND", None: "no route found"}[r.get("exists")]
        deny = " [never callable]" if r["route"] and al.DENY.search(r["route"]) else ""
        print(f"{r['name']:<36} {str(r['route'] or '-'):<44} {r['where']:<9} {mark}{deny}"
              + (f" allow={r['allow']}" if r.get("allow") else ""))

    print(f"\nlive {len(live)} · not on this backend {len(gone)} · no route found {len(none)}")

    if not ns.write:
        print("\n(nothing written; pass --write to put the live ones in Studio's allowlist)")
        return 0

    out, skipped = {}, []
    for r in live:
        if al.DENY.search(r["route"]):
            skipped.append(r["name"])
            continue
        s = r["schema"]
        e = {"method": "POST", "path": r["route"], "via": "submit", "kind": "compute",
             "required": list(s.get("required") or []), "optional": list(s.get("optional") or []),
             "approval": True, "timeout": 43200, "enabled": True,
             "why": (s.get("notes") or "").strip()[:300] or f"BioQL pipeline {r['name']} ({s.get('cost_tier') or '?'} cost).",
             "from_registry": {"name": r["name"], "version": s.get("version"), "cost_tier": s.get("cost_tier"),
                                "route_from": r["where"], "verified_http": r["http"],
                                # a derived route is proven to EXIST (405 names its methods), but the binding of this
                                # name to that route is inferred, not stated by BioQL. A person approves every call
                                # anyway, so a wrong binding cannot fire unattended — but it is marked, not hidden.
                                "route_confirmed_by_bioql": r["where"] in ("registry", "ui-map")}}
        bad = al.problems(al._norm(r["name"], e))
        if bad:
            skipped.append(f"{r['name']} ({'; '.join(bad)})")
            continue
        out[r["name"]] = e

    f = al.path_file()
    existing = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    merged = {**existing, **out}
    write_private(f, json.dumps(merged, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"\nwrote {len(out)} entries to {f}")
    if skipped:
        print("skipped:", ", ".join(skipped))
    print("every one needs a person's approval (kind=compute); none can be run by Claude.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
