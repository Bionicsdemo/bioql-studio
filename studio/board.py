"""The drug-design board: the plan drawn before anything is spent, and the evidence hung on it afterwards.

This is Iris's whiteboard recast for drug design. You say what you want; nodes appear; you confirm; only then does a
campaign run. The point is not the picture — it is that the board shows HOW it would be done before a QPU second or
a Modal dollar is spent, and that every conclusion on it reaches the raw call that produced it.

Node kinds, each from the brief:
  target      the protein or pathway (GLP-1R, DNA-PKcs, NRF2/SIRT1)
  mechanism   agonism, inhibition, co-crystal, photosensitisation
  candidate   a peptide, small molecule, antibody, mRNA
  evidence    one BioQL result that supports or contradicts a node — carries its ledger line, its provenance and
              the verifier's verdict, never a bare number
  risk        a known liability (pan-CYP, hERG), drawn in the warning colour
  plan        the sequence of BioQL calls that would settle a node, for a person to approve

The rules that make it honest rather than decorative:
  - an `evidence` node MUST name the ledger line it came from; there is no way to hang a number on the board by
    hand. A claim with no call behind it is not evidence, and the board will not hold one.
  - evidence carries the verifier's verdict with it. A node supported only by `contradicted` evidence is reported as
    contradicted, never as supported, and `supported` is three-state: yes, no, or unknown — "nothing checked it" is
    not "it failed".
  - a `plan` node is drawn, not run. Running it is a campaign, which needs a person.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from . import home, write_private
from .resolution import Resolution, absent, resolved, unresolvable

KINDS = ("target", "mechanism", "candidate", "evidence", "risk", "plan", "note")
# What a link means. The board stays readable because there are few of them.
EDGES = ("supports", "contradicts", "targets", "acts_on", "risks", "settles", "follows")
SUPPORTED, UNSUPPORTED, UNKNOWN = "supported", "unsupported", "unknown"


def boards_dir() -> Path:
    p = home() / "boards"
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


def path_of(board_id: str) -> Path:
    return boards_dir() / f"{board_id}.json"


def new(title: str, question: str = "") -> dict:
    b = {"id": uuid.uuid4().hex[:12], "title": title.strip() or "untitled board", "question": question.strip(),
         "created": round(time.time(), 3), "nodes": {}, "edges": [], "seq": 0}
    save(b)
    return b


def save(board: dict) -> dict:
    board["saved"] = round(time.time(), 3)
    write_private(path_of(board["id"]), json.dumps(board, indent=1, ensure_ascii=False, default=str) + "\n")
    return board


def load(board_id: str) -> Resolution:
    """Three-state: the board, ABSENT when there is none by that id, UNRESOLVABLE when the file exists and cannot be
    trusted — never an empty board, which would read as "it had nothing on it"."""
    f = path_of(board_id)
    if not f.exists():
        return absent("board store", f"no board {board_id!r}")
    try:
        b = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError) as why:
        return unresolvable("board store", f"{f.name}: {type(why).__name__}: {why}"[:200])
    if not isinstance(b, dict) or "nodes" not in b:
        return unresolvable("board store", f"{f.name}: not a board")
    return resolved(b, "board store", f"board {board_id}")


def boards() -> list[dict]:
    out = []
    for f in sorted(boards_dir().glob("*.json")):
        r = load(f.stem)
        if r.resolved:
            b = r.value
            out.append({"id": b["id"], "title": b["title"], "nodes": len(b["nodes"]), "saved": b.get("saved")})
        else:
            out.append({"id": f.stem, "title": None, "unreadable": r.detail})   # reported, never dropped
    return out


def add(board: dict, kind: str, label: str, **fields) -> dict:
    """Add a node. An `evidence` node without a ledger line is refused: the board holds no number that has no call
    behind it."""
    if kind not in KINDS:
        raise ValueError(f"unknown node kind {kind!r}; the board has {list(KINDS)}")
    if not str(label).strip():
        raise ValueError("a node needs a label")
    if kind == "evidence" and not str(fields.get("ledger_hash") or "").strip():
        raise ValueError("an evidence node must name the ledger line it came from; a number with no call behind it "
                         "is not evidence")
    board["seq"] += 1
    nid = f"n{board['seq']}"
    board["nodes"][nid] = {"id": nid, "kind": kind, "label": str(label).strip(), "at": round(time.time(), 3),
                           **fields}
    return board["nodes"][nid]


def link(board: dict, src: str, edge: str, dst: str) -> dict:
    if edge not in EDGES:
        raise ValueError(f"unknown link {edge!r}; the board has {list(EDGES)}")
    for n in (src, dst):
        if n not in board["nodes"]:
            raise ValueError(f"no node {n!r} on this board")
    e = {"from": src, "edge": edge, "to": dst}
    if e not in board["edges"]:
        board["edges"].append(e)
    return e


def evidence_from(board: dict, row: dict, label: str = "", about: str | None = None, contradicts: bool = False) -> dict:
    """Hang one ledger row on the board. Everything the reader needs to judge it travels with it: the tool, the
    verdict, the provenance real-or-null, and the hash that leads back to the raw call."""
    prov = row.get("provenance") or {}
    node = add(board, "evidence", label or f"{row.get('tool')} · {row.get('state')}",
               ledger_hash=row.get("hash", ""), tool=row.get("tool"), state=row.get("state"),
               verdict=row.get("verdict"), findings=row.get("findings") or [],
               is_real_hardware=prov.get("is_real_hardware"), ibm_job_ids=prov.get("ibm_job_ids") or [],
               result_sha256=row.get("result_sha256"), at_call=row.get("t"))
    if about:
        link(board, node["id"], "contradicts" if contradicts else "supports", about)
    return node


def support(board: dict, node_id: str) -> dict:
    """Is this node supported? Three states, and the reasons.

    `unknown` is not `unsupported`: a node nothing has checked has not failed. Only evidence the verifier called
    CLEAN counts as support; anything else — contradicted, concerns, or a verdict never recorded — is listed in
    `not_counted` with its reason, so a reader sees what was left out and why.
    """
    ok, against, tainted = [], [], []
    for e in board["edges"]:
        if e["to"] != node_id or e["edge"] not in ("supports", "contradicts"):
            continue
        n = board["nodes"].get(e["from"]) or {}
        if n.get("kind") != "evidence":
            continue
        # Only evidence the verifier explicitly called CLEAN can support a node. A row whose verdict was never
        # recorded says "not recorded", and reading that as clean is how an unchecked number becomes a conclusion —
        # the same degradation the three-state invariant forbids, one level up.
        if n.get("state") != "RESOLVED" or n.get("verdict") != "clean":
            tainted.append({"id": n["id"], "why": (f"state {n.get('state')}" if n.get("state") != "RESOLVED"
                                                   else f"verdict {n.get('verdict') or 'not recorded'}")})
        elif e["edge"] == "supports":
            ok.append(n["id"])
        else:
            against.append(n["id"])
    if against:
        state = UNSUPPORTED
    elif ok:
        state = SUPPORTED
    else:
        state = UNKNOWN
    return {"node": node_id, "state": state, "supported_by": ok, "contradicted_by": against,
            "not_counted": tainted,
            "why": ("evidence contradicts it" if against else
                    "evidence supports it" if ok else
                    f"{len(tainted)} piece(s) of evidence could not be counted" if tainted else
                    "nothing has checked it")}


def plan_nodes(board: dict) -> list[dict]:
    return [n for n in board["nodes"].values() if n["kind"] == "plan"]


def report(board: dict) -> dict:
    """What the board says, with the bad news first."""
    sup = {nid: support(board, nid) for nid, n in board["nodes"].items()
           if n["kind"] in ("target", "mechanism", "candidate")}
    findings = [f for n in board["nodes"].values() for f in (n.get("findings") or [])]
    highs = [f for f in findings if f.get("severity") == "high"]
    return {"id": board["id"], "title": board["title"], "question": board.get("question", ""),
            "nodes": len(board["nodes"]), "edges": len(board["edges"]),
            "contradicted": [n["id"] for n in board["nodes"].values() if n.get("verdict") == "contradicted"],
            "high_findings": len(highs), "support": sup,
            "unknown": [k for k, v in sup.items() if v["state"] == UNKNOWN],
            "unsupported": [k for k, v in sup.items() if v["state"] == UNSUPPORTED],
            "plans_awaiting_a_person": [n["id"] for n in plan_nodes(board)]}


def markdown(board: dict) -> str:
    """The board as a page a reader can hand to someone else. The warnings are at the top, never in a footnote."""
    r = report(board)
    out = [f"# {board['title']}", ""]
    if board.get("question"):
        out += [board["question"], ""]
    if r["contradicted"] or r["high_findings"]:
        out += [f"**{r['high_findings']} high finding(s); {len(r['contradicted'])} piece(s) of evidence the verifier "
                f"calls contradicted.** Read those before anything else on this board.", ""]
    if r["unsupported"]:
        out += [f"**Contradicted by evidence:** {', '.join(board['nodes'][k]['label'] for k in r['unsupported'])}", ""]
    for kind in ("target", "mechanism", "candidate", "risk", "plan"):
        rows = [n for n in board["nodes"].values() if n["kind"] == kind]
        if not rows:
            continue
        out.append(f"## {kind.title()}")
        for n in rows:
            s = r["support"].get(n["id"])
            mark = f" — {s['state']}: {s['why']}" if s else ""
            out.append(f"- **{n['label']}**{mark}")
        out.append("")
    ev = [n for n in board["nodes"].values() if n["kind"] == "evidence"]
    if ev:
        out += ["## Evidence", "", "| tool | state | verdict | real hardware | IBM job | ledger |",
                "| --- | --- | --- | --- | --- | --- |"]
        for n in ev:
            hw = "—" if n.get("is_real_hardware") is None else str(n.get("is_real_hardware"))
            job = ", ".join(n.get("ibm_job_ids") or []) or "—"
            out.append(f"| {n.get('tool')} | {n.get('state')} | {n.get('verdict') or 'not recorded'} | {hw} | {job} "
                       f"| `{str(n.get('ledger_hash'))[:12]}` |")
        out.append("")
        out.append("Every row leads back to its call: the ledger hash is the whole record of what was asked and what "
                   "came back.")
    return "\n".join(out)
