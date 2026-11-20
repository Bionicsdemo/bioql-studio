"""The adapter as an MCP server (stdio, JSON-RPC 2.0): how Claude Code sees BioQL.

Claude gets one tool per enabled allowlist entry, with BioQL's parameters, and nothing else: no code, no files, no
route it can name. A call goes through Adapter.call with `by="claude"` and no approval, so anything that could reach a
real QPU, or that the owner marked for approval, is refused here by construction.

Two things make a run of independent work take the time of its slowest step instead of the sum of all of them:

- **`run_many`**, one extra tool: a list of calls that do not need each other's answers, run at the same time, each
  with its own gate, state, verdict and line on the record (`studio/fanout.py`).
- **A concurrent loop.** Every `tools/call` is served in its own thread and the answer is written when it is ready.
  JSON-RPC matches answers by `id`, so arriving out of order is correct. Serving them one at a time meant a
  submitted pipeline held the pipe for as long as it ran — with a 12-hour ceiling, Claude could not even ask whether
  the backend was alive while one was going.
"""
from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from . import NAME, VERSION
from . import claude_tools
from .adapter import Adapter, Refused
from .fanout import MAX_CALLS, run_many

IN_FLIGHT = 8          # how many of Claude's calls may be in BioQL at once, over all its turns
MAX_TEXT = 200_000     # how much of one answer fits in one message; beyond it the answer is DESCRIBED, never cut

RUN_MANY = {
    "name": "run_many",
    "description": ("[batch] Run several BioQL tools AT THE SAME TIME. Use it whenever the next calls do not need "
                    "each other's answers — a depiction, a conformer, a health read and a descriptor are four "
                    "independent questions and take one wait, not four. Each call keeps its own approval gate, its "
                    "own state (RESOLVED / ABSENT / UNRESOLVABLE / REFUSED), its own verdict and its own line on "
                    f"the record; one failing does not touch the others. At most {MAX_CALLS} per batch. A call that "
                    "needs an earlier answer goes in a LATER batch — this never reorders or waits for you."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "calls": {
                "type": "array", "minItems": 1, "maxItems": MAX_CALLS,
                "description": "The independent calls, in the order you want them reported back.",
                "items": {"type": "object",
                          "properties": {"name": {"type": "string", "description": "a tool from this server"},
                                         "args": {"type": "object", "description": "that tool's arguments"}},
                          "required": ["name"]},
            },
        },
        "required": ["calls"], "additionalProperties": False,
    },
}


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
    out.append(dict(RUN_MANY))
    # And the instrument itself. Claude had 502 doors into BioQL and none into the record around it: it could run
    # a pipeline and not ask what had already been run, be told a result contradicted itself and have no way to
    # see whether the same thing contradicted last week, and had no way to read a 190 KB answer without pulling
    # all of it into the conversation. See studio/claude_tools.py.
    out += [dict(t) for t in claude_tools.TOOLS]
    return out


def _called(ad: Adapter, p: dict) -> tuple[str, bool]:
    """(the text Claude sees, whether it is an error)."""
    name = str(p.get("name"))
    args = p.get("arguments") or {}
    try:
        if name == "run_many":
            out = run_many(ad, args.get("calls"))
            # The batch is an error only if NOTHING in it resolved: a batch where four of five answered is not a
            # failure, and reporting it as one would hide the four.
            bad = not any(r.get("state") == "RESOLVED" for r in out["runs"])
            return json.dumps(out, ensure_ascii=False, default=str), bad
        if name in claude_tools.NAMES:
            out = claude_tools.handle(name, args, ad)
            return json.dumps(out, ensure_ascii=False, default=str), bool(out.get("error"))
        res = ad.call(name, args, approved_by=None, by="claude")
        return json.dumps(res.as_dict(), ensure_ascii=False, default=str), not res.resolved and not res.absent
    except Refused as why:
        return json.dumps({"state": "REFUSED", "detail": str(why)}), True


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
            text, err = _called(ad, msg.get("params") or {})
            # An answer that does not fit is SAID, not cut. `text[:200_000]` handed the brain JSON chopped in the
            # middle of an object, with nothing to say why — the same failure this project refuses everywhere
            # else: at the limit, produce something plausible instead of stopping. Now it keeps the state, the
            # verdict, the findings and the provenance, and names the tools that read the rest in pieces.
            if len(text) > MAX_TEXT:
                text, err = claude_tools.too_big(text, MAX_TEXT), True
            result = {"content": [{"type": "text", "text": text}], "isError": err}
        elif method == "ping":
            result = {}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}}
    except Exception as e:                            # the server stays up; the caller sees the failure
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"[:500]}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    ad = Adapter()
    out_lock = threading.Lock()
    pool = ThreadPoolExecutor(max_workers=IN_FLIGHT, thread_name_prefix="mcp")

    def answer(msg: dict) -> None:
        out = handle(ad, msg)
        if out is None:
            return
        line = json.dumps(out, ensure_ascii=False, default=str) + "\n"
        with out_lock:                                # one writer at a time: two interleaved lines are neither
            sys.stdout.write(line)
            sys.stdout.flush()

    try:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                with out_lock:
                    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                                 "error": {"code": -32700, "message": "parse error"}}) + "\n")
                    sys.stdout.flush()
                continue
            # `initialize` and `tools/list` are cheap and the session is built on them, so they are answered on this
            # thread, in order. Only the work fans out.
            if msg.get("method") == "tools/call":
                pool.submit(answer, msg)
            else:
                answer(msg)
    finally:
        pool.shutdown(wait=True)


if __name__ == "__main__":
    main()
