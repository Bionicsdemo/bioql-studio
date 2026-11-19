"""bioql-studio command line.

  python -m studio tools                         the entries Studio may call
  python -m studio registry                      BioQL's own pipeline registry, and which entries could be allowed
  python -m studio allow NAME [--kind K]         add one registry pipeline to the allowlist (its schema copied from BioQL)
  python -m studio call NAME '{"k": "v"}' [--approve WHO]
  python -m studio guard snapshot NAME | guard check NAME
  python -m studio ledger verify
  python -m studio mcp                           serve the adapter to Claude Code over stdio
"""
from __future__ import annotations

import argparse
import json
import sys

from . import allowlist as al
from . import guard, ledger
from .adapter import Adapter, Refused


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bioql-studio")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tools")
    sub.add_parser("registry")
    a = sub.add_parser("allow"); a.add_argument("name"); a.add_argument("--kind", choices=al.KINDS)
    c = sub.add_parser("call"); c.add_argument("name"); c.add_argument("args", nargs="?", default="{}")
    c.add_argument("--approve", metavar="WHO", help="a person approves this call (needed for hardware or marked entries)")
    g = sub.add_parser("guard"); g.add_argument("action", choices=("snapshot", "check")); g.add_argument("name")
    lg = sub.add_parser("ledger"); lg.add_argument("action", choices=("verify",))
    sub.add_parser("mcp")
    ns = ap.parse_args(argv)

    if ns.cmd == "mcp":
        from .mcp_server import main as serve
        serve(); return 0
    if ns.cmd == "tools":
        for e in Adapter(log=False).tools():
            flag = " (approval)" if e["approval"] or e["kind"] == "hardware" else ""
            print(f"{e['name']:<22} {e['method']:<4} {e['path']:<40} {e['kind']}{flag}")
        return 0
    if ns.cmd == "registry":
        res = Adapter(log=False).call("tools_registry")
        if not res.resolved:
            print(json.dumps(res.as_dict(), indent=1)); return 2
        mine = {e["path"] for e in Adapter(log=False).entries.values()}
        for t in res.value.get("tools", []):
            s = t.get("schema") or {}
            ok = t.get("kind") == "http" and t.get("endpoint")
            mark = "in allowlist" if t.get("endpoint") in mine else ("allowable" if ok and not al.DENY.search(t["endpoint"]) else "not http")
            print(f"{t.get('name'):<34} {str(t.get('endpoint')):<44} {s.get('cost_tier') or '-':<8} {mark}")
        return 0
    if ns.cmd == "allow":
        res = Adapter(log=False).call("tools_registry")
        tool = next((t for t in res.value.get("tools", []) if t.get("name") == ns.name), None) if res.resolved else None
        if tool is None:
            print(f"{ns.name!r} is not in BioQL's registry ({res.state})"); return 2
        e = al.save_entry(ns.name, al.from_registry(tool, ns.kind))
        print(json.dumps(e, indent=1)); return 0
    if ns.cmd == "call":
        try:
            args = json.loads(ns.args)
        except ValueError as why:
            print(json.dumps({"state": "REFUSED", "detail": f"arguments are not JSON: {why}"})); return 3
        try:
            res = Adapter().call(ns.name, args, approved_by=ns.approve, by="cli")
        except Refused as why:
            print(json.dumps({"state": "REFUSED", "detail": str(why)})); return 3
        print(json.dumps(res.as_dict(), indent=1, ensure_ascii=False, default=str)[:20000])
        return 0 if res.resolved else 1
    if ns.cmd == "guard":
        if ns.action == "snapshot":
            print(guard.save(ns.name, guard.snapshot())); return 0
        rep = guard.compare(guard.load(ns.name), guard.snapshot())
        print(json.dumps(rep, indent=1)[:20000]); return 0 if rep["clean"] else 4
    if ns.cmd == "ledger":
        r = ledger.verify(); print(json.dumps(r)); return 0 if r["ok"] else 5
    return 1


if __name__ == "__main__":
    sys.exit(main())
