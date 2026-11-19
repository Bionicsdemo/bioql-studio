"""The adapter as an MCP server (stdio, JSON-RPC 2.0): how Claude Code sees BioQL.

Claude gets one tool per enabled allowlist entry, with BioQL's parameters, and nothing else: no code, no files, no
route it can name. A call goes through Adapter.call with `by="claude"` and no approval, so anything that could reach a
real QPU, or that the owner marked for approval, is refused here by construction.
"""
from __future__ import annotations

import json
import sys

from . import NAME, VERSION
from .adapter import Adapter, Refused


def _schema(e: dict) -> dict:
    props = {k: {"type": "string"} if k in ("smiles", "job_id", "target", "format") else {}
             for k in e["required"] + e["optional"]}
    for p in __import__("re").findall(r"\{([a-z_][a-z0-9_]*)\}", e["path"]):
        props[p] = {"type": "string"}
    return {"type": "object", "properties": props, "required": list(dict.fromkeys(e["required"])),
            "additionalProperties": False}


def tool_list(ad: Adapter) -> list[dict]:
    out = []
    for e in ad.tools():
        note = " Needs a person's approval; Claude cannot run it." if e["approval"] or e["kind"] == "hardware" else ""
        out.append({"name": e["name"], "description": f"[{e['kind']}] {e['why']}{note}".strip(), "inputSchema": _schema(e)})
    return out


def handle(ad: Adapter, msg: dict) -> dict | None:
    mid, method = msg.get("id"), msg.get("method")
    if mid is None:                                   # a notification: no answer
        return None
    try:
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": NAME, "version": VERSION}}
        elif method == "tools/list":
            result = {"tools": tool_list(ad)}
        elif method == "tools/call":
            p = msg.get("params") or {}
            try:
                res = ad.call(str(p.get("name")), p.get("arguments") or {}, approved_by=None, by="claude")
                text, err = json.dumps(res.as_dict(), ensure_ascii=False, default=str), not res.resolved and not res.absent
            except Refused as why:
                text, err = json.dumps({"state": "REFUSED", "detail": str(why)}), True
            result = {"content": [{"type": "text", "text": text[:200_000]}], "isError": err}
        elif method == "ping":
            result = {}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}}
    except Exception as e:                            # the server stays up; the caller sees the failure
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"[:500]}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    ad = Adapter()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            out = handle(ad, msg)
        if out is not None:
            sys.stdout.write(json.dumps(out, ensure_ascii=False, default=str) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
