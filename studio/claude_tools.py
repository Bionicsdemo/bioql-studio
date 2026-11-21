"""What Claude can do with Studio ITSELF, beside what it can do with BioQL.

Until now Claude had 502 doors into BioQL and not one into the instrument around it. It could run a pipeline and
never ask what had already been run; it could be told a result contradicted itself and have no way to see whether
the same thing had contradicted last week; it could not write down a plan, and the evidence board it was supposed
to fill was reachable only from the window. And when an answer was large it was cut at 200 000 characters, in the
middle of the JSON, with nothing saying so — the brain received a broken object and no reason.

These tools close that. Every one of them is about Studio's own record or Studio's own reading of a result:

  studio_record     the signed, hash-chained record: what has been called, with what verdict
  studio_findings   every verdict the verifier wrote, worst first — the instrument's own judgement
  studio_usage      what has been spent, counted off that record rather than off a tally beside it
  studio_sections   a large result as its SECTIONS, with sizes — so a 190 KB answer costs a list, not 190 KB
  studio_section    one of those sections, in full
  studio_board      the whiteboard: the plan drawn before anything is spent
  studio_board_write  put a node, a link or a piece of EVIDENCE on it

Three rules hold across all of them, and they are the reason this file is small:

1. **Nothing here reaches BioQL.** They read what Studio already wrote down, or re-read a result through the same
   allowlisted route any caller would use. There is no second path to BioQL and this file does not open one.
2. **Nothing here can invent evidence.** `studio_board_write` with `act: "evidence"` takes a ledger HASH and the row
   is read from the record. A number with no call behind it cannot get onto the board through this door.
3. **The record is read-only from here.** Claude can read every line and write none: lines are written by making
   calls, which is the only thing that should ever put one there.
"""
from __future__ import annotations

import json
import time
from typing import Any

from . import board, ledger, usage

# The last few big payloads this process has read, so `studio_section` can serve one section without asking BioQL
# again. Bounded: a cache that grows is a memory leak with a nice name.
_SEEN: dict[str, dict] = {}
_SEEN_MAX = 4

MAX_ROWS = 200


def _rows(limit: int) -> list[dict]:
    f = ledger.path_file()
    if not f.exists():
        return []
    out = []
    for line in f.read_bytes().splitlines()[-max(1, min(limit, MAX_ROWS)):]:
        try:
            r = json.loads(line)
        except ValueError:
            out.append({"state": "UNREADABLE", "note": "this line is not JSON; the chain check will say so"})
            continue
        v = r.get("value")
        if isinstance(v, (dict, list)):     # the record holds hashes, never payloads
            r["value"] = f"<{type(v).__name__}>"
        r.pop("args", None)                 # arguments can be long and are not what this is for
        out.append(r)
    return out


TOOLS: list[dict] = [
    {
        "name": "studio_record",
        "description": ("[record] What has ALREADY been called, from Studio's hash-chained, signed record — newest "
                        "last. Use it before running anything expensive: the same question may already have an "
                        "answer, and a tool that failed the same way twice is worth knowing about. Carries no "
                        "payloads (the record holds their sha256) — use studio_sections to read a result."),
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": f"how many recent lines, up to {MAX_ROWS} (default 50)"},
            "tool": {"type": "string", "description": "only lines for this tool name"},
            "state": {"type": "string", "description": "RESOLVED, ABSENT, UNRESOLVABLE or REFUSED"},
            "verdict": {"type": "string", "description": "clean, concerns, contradicted, or 'flagged' for any that "
                                                         "is not clean"},
        }, "additionalProperties": False},
    },
    {
        "name": "studio_findings",
        "description": ("[record] The verifier's own verdicts, worst first. Every result Studio receives is checked "
                        "for contradicting itself — a quantum label over classical execution, hardware claimed with "
                        "no job id, a determinant-selection result with no dim/dim_FCI. This is how you find out "
                        "whether what you are about to report has already been contradicted."),
        "inputSchema": {"type": "object", "properties": {
            "only_flagged": {"type": "boolean", "description": "only what is NOT clean (default true)"},
            "limit": {"type": "integer"},
        }, "additionalProperties": False},
    },
    {
        "name": "studio_usage",
        "description": "[record] What has been spent and run, counted off the signed record rather than a tally kept beside it.",
        "inputSchema": {"type": "object", "properties": {
            "days": {"type": "integer", "description": "how far back (default 30)"}}, "additionalProperties": False},
    },
    {
        "name": "studio_sections",
        "description": ("[reading] A large result broken into the SECTIONS BioQL's own window draws, with the size "
                        "of each. Give it the JSON of a result you already have, or a session id to re-read. Use "
                        "this instead of pulling a whole pipeline answer into the conversation: a real one is 130 KB "
                        "to 1 MB, and most of it is one embedded structure. Then ask for the section you need."),
        "inputSchema": {"type": "object", "properties": {
            "session_id": {"type": "string", "description": "a recovery session id, e.g. ps-bd5284e25a0e"},
            "payload": {"type": "string", "description": "or the result's JSON, as text"},
        }, "additionalProperties": False},
    },
    {
        "name": "studio_section",
        "description": ("[reading] One section of a result that studio_sections listed, in full, as the blocks the "
                        "window draws: tables, facts, charts, gates, structures. Ask for them one at a time."),
        "inputSchema": {"type": "object", "properties": {
            "session_id": {"type": "string"},
            "section": {"type": "string", "description": "the section id from studio_sections"},
        }, "required": ["section"], "additionalProperties": False},
    },
    {
        "name": "studio_board",
        "description": ("[plan] The whiteboard: the plan drawn before anything is spent, and the evidence hung on "
                        "it. With no id, lists the boards. With one, gives its nodes, its links and its report — "
                        "which claims are supported, which are contradicted, and which are still unknown."),
        "inputSchema": {"type": "object", "properties": {
            "id": {"type": "string", "description": "a board id; omit to list them"}}, "additionalProperties": False},
    },
    {
        "name": "studio_board_write",
        "description": ("[plan] Put something on a whiteboard. `new` starts one; `node` adds a target, mechanism, "
                        "candidate, risk, plan or note; `link` joins two of them (supports, contradicts, targets, "
                        "acts_on, risks, settles, follows); `evidence` hangs a LINE OF THE RECORD on it by its hash "
                        "— and only by its hash, so nothing reaches the board without a call behind it."),
        "inputSchema": {"type": "object", "properties": {
            "act": {"type": "string", "description": "new | node | link | evidence"},
            "id": {"type": "string", "description": "the board (not needed for `new`)"},
            "title": {"type": "string"}, "question": {"type": "string"},
            "kind": {"type": "string"}, "label": {"type": "string"},
            "from": {"type": "string"}, "edge": {"type": "string"}, "to": {"type": "string"},
            "ledger_hash": {"type": "string", "description": "for `evidence`: the hash of the line it rests on"},
            "about": {"type": "string", "description": "for `evidence`: the node id it speaks about"},
            "contradicts": {"type": "boolean"},
        }, "required": ["act"], "additionalProperties": False},
    },
    {
        "name": "studio_contribute",
        "description": (
            "[completa] Donde BioQL dice que NO puede calcular algo y tu si sabes algo util, ponlo aqui: pasa "
            "a formar parte de LA MISMA respuesta, con su fuente. BioQL declara sus huecos con su razon, y no "
            "todos son iguales: a unos les falta una ENTRADA (\"need receptor sequence to fold the complex\") y "
            "esos NO se completan, se CIERRAN -- consigue el dato, vuelve a llamar, y el numero volvera medido. "
            "Otros no tienen MODELO (\"no validated per-molecule dose model\"): ahi no hay numero que BioQL "
            "pueda dar y lo que cabe es lo que sepas de fuera. "
            "`basis` es OBLIGATORIO y se refusa sin el: un valor sin fuente es exactamente el defecto que este "
            "proyecto ya envio una vez, tres dosis distintas para la misma molecula. El alcance va DENTRO del "
            "propio `value` (\"~10% de fase 1 a aprobacion en oncologia\"), porque eso es informacion y no una "
            "advertencia. No lo anuncies ni lo presentes como tuyo: entrega el resultado."),
        "inputSchema": {"type": "object", "properties": {
            "what": {"type": "string", "description": "sobre que es: 'probabilidad de exito clinico', 'riesgo de agregacion'"},
            "value": {"type": "string", "description": "el valor, en palabras. Si es un numero, con su unidad y su rango"},
            "basis": {"type": "string", "description": "la fuente: un articulo con su cita, una guia, una tasa de "
                                                       "clase publicada. Sin fuente no hay valor que poner"},
            "not_a": {"type": "string", "description": "opcional, y solo si hace falta: el alcance que el valor no "
                                                       "cubre ('tasa de la clase, no de este peptido')"},
            "about_gap": {"type": "string", "description": "el hueco que contesta, citado de BioQL"},
        }, "required": ["what", "value", "basis"], "additionalProperties": False},
    },
]

NAMES = {t["name"] for t in TOOLS}


def _payload_of(args: dict, ad) -> tuple[Any, str | None]:
    """The result to read: one given as text, or one re-read through the allowlist. Returns (payload, error)."""
    sid = str(args.get("session_id") or "").strip()
    raw = args.get("payload")
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw), None
        except ValueError as why:
            return None, f"that payload is not JSON: {why}"
    if not sid:
        return None, "give either a session_id or a payload"
    if sid in _SEEN:
        return _SEEN[sid], None
    res = ad.call("recovery_session_id_result", {"id": sid}, approved_by=None, by="claude")
    if not res.resolved:
        return None, f"{res.state}: {res.detail}"
    while len(_SEEN) >= _SEEN_MAX:
        _SEEN.pop(next(iter(_SEEN)))
    _SEEN[sid] = res.value
    return res.value, None


def handle(name: str, args: dict, ad) -> dict:
    """One of these tools, run. Always a dict; never a raised exception reaching the protocol layer."""
    from . import cards

    if name == "studio_record":
        rows = _rows(int(args.get("limit") or 50))
        tool, state, verdict = args.get("tool"), args.get("state"), args.get("verdict")
        if tool:
            rows = [r for r in rows if r.get("tool") == tool]
        if state:
            rows = [r for r in rows if r.get("state") == str(state).upper()]
        if verdict == "flagged":
            rows = [r for r in rows if r.get("verdict") and r["verdict"] != "clean"]
        elif verdict:
            rows = [r for r in rows if r.get("verdict") == verdict]
        return {"rows": rows, "chain": ledger.verify(),
                "note": "the chain's own verdict travels with the rows: lines nobody can verify are not evidence"}

    if name == "studio_findings":
        only = args.get("only_flagged", True)
        rows = [r for r in _rows(int(args.get("limit") or 100)) if r.get("verdict") or r.get("findings")]
        if only:
            rows = [r for r in rows if r.get("verdict") and r["verdict"] != "clean"]
        rows.reverse()
        return {"findings": rows, "counted": len(rows),
                "note": "a clean verdict means nothing in the answer contradicted ITSELF. It is not a statement "
                        "that the science is right."}

    if name == "studio_usage":
        # Counted off the signed record, which is the only place a call is written. `usage.jsonl` meters MODEL
        # calls and is ABSENT here by design: Studio's brain is Claude Code, which this process does not bill.
        since = time.time() - 86400 * int(args.get("days") or 30)
        rows = [r for r in _rows(MAX_ROWS) if float(r.get("t") or 0) >= since]
        by_tool: dict[str, int] = {}
        by_state: dict[str, int] = {}
        by_verdict: dict[str, int] = {}
        seconds = 0.0
        for r in rows:
            by_tool[str(r.get("tool"))] = by_tool.get(str(r.get("tool")), 0) + 1
            by_state[str(r.get("state"))] = by_state.get(str(r.get("state")), 0) + 1
            by_verdict[str(r.get("verdict"))] = by_verdict.get(str(r.get("verdict")), 0) + 1
            seconds += float(r.get("seconds") or 0)
        metered = usage.read_rows()
        return {"calls": len(rows), "seconds_in_bioql": round(seconds, 1), "by_tool": by_tool,
                "by_state": by_state, "by_verdict": by_verdict,
                "window": f"the last {int(args.get('days') or 30)} day(s), within the last {MAX_ROWS} lines",
                "model_metering": metered.detail if not metered.resolved else f"{len(metered.value)} metered call(s)"}

    if name == "studio_sections":
        payload, bad = _payload_of(args, ad)
        if bad:
            return {"error": bad}
        value = payload.get("result", payload) if isinstance(payload, dict) else payload
        # `sections` yields (id, title, the path it was shown from, the subtree). The bytes are the SUBTREE's, which
        # is what asking for that section would actually cost.
        out = [{"id": sid, "title": title, "shown_from": path,
                "bytes": len(json.dumps(sub_, default=str))} for sid, title, path, sub_ in cards.sections(value)]
        whole = len(json.dumps(value, default=str))
        return {"sections": out, "total_bytes": whole,
                "biggest_keys": sorted(((k, len(json.dumps(v, default=str))) for k, v in (value or {}).items()
                                        if isinstance(value, dict)), key=lambda kv: -kv[1])[:6],
                "note": "ask for one with studio_section. The whole result is `total_bytes`; in a real pipeline "
                        "answer most of it is usually one embedded structure, listed in biggest_keys."}

    if name == "studio_section":
        payload, bad = _payload_of(args, ad)
        if bad:
            return {"error": bad}
        value = payload.get("result", payload) if isinstance(payload, dict) else payload
        want = str(args.get("section") or "")
        secs = cards.sections(value)
        hit = next((t for t in secs if t[0] == want), None)
        if hit is None:
            return {"error": f"no section {want!r} in this result", "available": [t[0] for t in secs]}
        sid, title, path, sub_ = hit
        # The BLOCKS the window draws for that section — tables, facts, charts, gates, structures — not the raw
        # subtree, so what Claude reads and what a person sees are the same reading of the same bytes.
        group = cards.cards_of(value, tool=str(args.get("session_id") or "result"), meta={})
        cards_list = group.get("cards") if group.get("kind") == "group" else [group]
        card = next((c for c in (cards_list or []) if c.get("section") == sid), None)
        return {"id": sid, "title": title, "shown_from": path,
                "blocks": (card or {}).get("blocks"),
                "subtree": sub_ if len(json.dumps(sub_, default=str)) < 60_000 else "<too large to inline>"}

    if name == "studio_board":
        bid = str(args.get("id") or "").strip()
        if not bid:
            return {"boards": board.boards()}
        r = board.load(bid)
        if not r.resolved:
            return {"state": r.state, "detail": r.detail}
        b = r.value
        return {"board": b, "report": board.report(b), "markdown": board.markdown(b)}

    if name == "studio_contribute":
        what = str(args.get("what") or "").strip()
        value = str(args.get("value") or "").strip()
        basis = str(args.get("basis") or "").strip()
        if not (what and value and basis):
            # La base es la unica regla que hace esto seguro. Sin ella, un aporte es indistinguible de un
            # numero inventado, que es el defecto que esta app existe para no repetir.
            return {"state": "REFUSED",
                    "detail": "un aporte necesita `what`, `value` y `basis`. Sin decir de donde sale no se "
                              "distingue de un numero inventado, y eso ya paso una vez en este proyecto"}
        # Sin defecto a proposito. Un alcance que nadie escribio seria una frase puesta por la app, y la que
        # habia ("no es una medida de BioQL") marcaba de quien era el valor -- justo lo que no se marca.
        not_a = str(args.get("not_a") or "").strip()
        gap = str(args.get("about_gap") or "").strip()
        linea = ledger.append({"tool": "studio_contribute", "state": "RESOLVED", "by": "claude",
                               "verdict": None, "detail": what[:200],
                               "args": {"what": what, "basis": basis, "about_gap": gap},
                               "contribution": {"what": what, "value": value, "basis": basis,
                                                "not_a": not_a, "about_gap": gap}})
        # La tarjeta no dice de quien es el valor: se llama por lo que es y lleva su fuente. El hueco que
        # contesta NO vuelve a salir aqui -- ya esta en la tarjeta del resultado, y repetirlo justo encima de su
        # respuesta la contradice. Queda en el registro, que es donde sirve.
        return {"state": "RESOLVED", "recorded": True,
                "ledger_hash": (linea or {}).get("hash") if isinstance(linea, dict) else None,
                "card": {"kind": "result", "title": what, "subtitle": "",
                         "provenance": {"is_real_hardware": None, "ibm_job_ids": [], "backends": []},
                         "verdict": None, "findings": [],
                         "blocks": [{k: v for k, v in
                                     {"type": "contributed", "what": what, "value": value,
                                      "basis": basis, "not_a": not_a}.items() if v}]},
                "note": "queda en el registro firmado con su fuente; en la respuesta se lee como una fila mas"}

    if name == "studio_board_write":
        act = str(args.get("act") or "")
        try:
            if act == "new":
                made = board.new(str(args.get("title") or ""), str(args.get("question") or ""))
                return {"board": made["id"], "note": "give this id to studio_board to read it back"}
            r = board.load(str(args.get("id") or ""))
            if not r.resolved:
                return {"state": r.state, "detail": r.detail}
            bd = r.value
            if act == "node":
                node = board.add(bd, str(args.get("kind") or ""), str(args.get("label") or ""))
            elif act == "link":
                board.link(bd, str(args.get("from") or ""), str(args.get("edge") or ""), str(args.get("to") or ""))
                node = {"ok": True}
            elif act == "evidence":
                want = str(args.get("ledger_hash") or "")
                row = next((x for x in _rows(MAX_ROWS) if x.get("hash") == want), None)
                if row is None:
                    # The one thing this door must never do: accept a number with no call behind it.
                    return {"error": "that line is not on the hash-chained record. Evidence names a ledger hash "
                                     "and the row is read from the record, never from this request."}
                node = board.evidence_from(bd, row, about=(str(args["about"]) if args.get("about") else None),
                                           contradicts=bool(args.get("contradicts")))
            else:
                return {"error": f"unknown act {act!r}: it is new, node, link or evidence"}
            board.save(bd)
            return {"node": node, "report": board.report(bd)}
        except ValueError as why:
            return {"error": str(why)}

    return {"error": f"no such tool {name!r}"}


def too_big(text: str, cap: int) -> str:
    """What to send when an answer does not fit, instead of the first `cap` characters of it.

    Cutting JSON in the middle hands the brain an object that will not parse and no reason why — the exact shape of
    failure this project refuses everywhere else: at the limit, produce something plausible instead of stopping.
    """
    head = text[:400]
    try:
        d = json.loads(text)
        meta = d.get("meta") or {}
        keep = {"state": d.get("state"), "source": d.get("source"), "detail": d.get("detail"),
                "verdict": meta.get("verdict"), "findings": meta.get("findings"),
                "provenance": meta.get("provenance"), "seconds": meta.get("seconds")}
    except ValueError:
        keep = {"state": "UNREADABLE", "head": head}
    keep["truncated"] = {
        "bytes": len(text), "cap": cap,
        "why": "this answer is larger than one message may carry, so it is NOT included — a result cut in the "
               "middle is not a smaller result, it is a broken one.",
        "what_to_do": "read it in pieces: studio_sections lists its sections and their sizes, studio_section gives "
                      "you one. If it came from a session, pass that session_id.",
    }
    return json.dumps(keep, ensure_ascii=False, default=str)
