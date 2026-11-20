"""bioql-studio command line.

  python -m studio tools                         the entries Studio may call
  python -m studio registry                      BioQL's own pipeline registry, and which entries could be allowed
  python -m studio allow NAME [--kind K]         add one registry pipeline to the allowlist (its schema copied from BioQL)
  python -m studio call NAME '{"k": "v"}' [--approve WHO]
  python -m studio guard snapshot NAME | guard check NAME
  python -m studio ledger verify
  python -m studio mcp                           serve the adapter to Claude Code over stdio
  python -m studio release key                   make (or show) the Ed25519 key that signs releases
  python -m studio release sign DMG --version V  write latest.json and its signature beside the image
  python -m studio release check                 ask the channel what it publishes, and verify the signature
  python -m studio connect [--remove|--status]   plug BioQL into every Claude on this Mac, over MCP
  python -m studio refresh                       backfill what BioQL says about each entry, from its registry
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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
    r = sub.add_parser("release"); r.add_argument("action", choices=("key", "sign", "check"))
    r.add_argument("dmg", nargs="?"); r.add_argument("--version")
    cn = sub.add_parser("connect")
    cn.add_argument("--remove", action="store_true", help="take BioQL out of those clients again")
    cn.add_argument("--status", action="store_true", help="only say where it is plugged in")
    sub.add_parser("refresh")
    ns = ap.parse_args(argv)

    if ns.cmd == "connect":
        from . import connect as cn
        if ns.status:
            for row in cn.status():
                print(f"{row['client']:<16} {row['state']:<15} {row['detail']}")
            return 0
        res = cn.apply(remove=ns.remove)
        print(json.dumps(res.as_dict(), indent=1, default=str))
        return 0 if res.resolved or res.absent else 2

    if ns.cmd == "refresh":
        from . import allowlist as al2
        res = Adapter(log=False).call("tools_registry")
        if not res.resolved:
            print(json.dumps(res.as_dict(), indent=1)); return 2
        print(json.dumps(al2.refresh(res.value.get("tools") or []), indent=1, ensure_ascii=False))
        return 0

    if ns.cmd == "release":
        from . import release as rel
        if ns.action == "key":
            # Making it is one act; the public half is what goes in the source. The private half is never printed.
            sk = rel.private_key(create=True)
            print(f"private key: {rel.key_path()} (0600 — this file is the key to every Mac running this app)")
            print(f"public key:  {rel.public_hex(sk)}")
            print(f"pinned in studio/release.py: {rel.PUBLIC_KEY}")
            if rel.public_hex(sk) != rel.PUBLIC_KEY:
                print("THEY DO NOT MATCH. Builds made from this source will refuse releases signed with this key.")
                return 2
            return 0
        if ns.action == "sign":
            if not ns.dmg or not ns.version:
                print("release sign needs a .dmg and --version"); return 2
            dmg = Path(ns.dmg)
            man = rel.manifest_for(dmg, ns.version)
            sig = rel.sign(man)
            (dmg.parent / rel.MANIFEST).write_text(json.dumps(man, sort_keys=True, separators=(",", ":")) + "\n")
            (dmg.parent / rel.SIGNATURE).write_text(sig + "\n")
            print(json.dumps(man, indent=1))
            print(f"signature: {sig[:32]}…")
            print(f"wrote {dmg.parent / rel.MANIFEST} and {dmg.parent / rel.SIGNATURE}")
            print(f"verified against the pinned key: {rel.verify(man, sig)}")
            return 0
        # The running version has ONE source, `app/__init__.py`, which `build.sh` also reads. Imported here and
        # not at the top: `studio` is the layer below `app` and must keep working without it.
        from app import VERSION
        res = rel.latest(VERSION)
        print(json.dumps(res.as_dict(), indent=1, default=str))
        return 0 if res.resolved or res.absent else 2

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
        d = res.as_dict()
        d["verdict"], d["findings"] = res.meta.get("verdict"), res.meta.get("findings")
        d["provenance"] = res.meta.get("provenance")
        text = json.dumps(d, indent=1, ensure_ascii=False, default=str)
        if len(text) > 20000:          # never print half a JSON document: a cut object is not readable data
            v = d.pop("value", None)
            d["value_omitted"] = {"bytes": len(json.dumps(v, default=str)),
                                  "keys": sorted(v)[:40] if isinstance(v, dict) else type(v).__name__,
                                  "note": "too large to print; read it from the ledger or call it from Python"}
            text = json.dumps(d, indent=1, ensure_ascii=False, default=str)
        print(text)
        for f in res.meta.get("findings") or []:
            print(f"  [{f['severity']}] {f['code']}: {f['detail']}", file=sys.stderr)
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
