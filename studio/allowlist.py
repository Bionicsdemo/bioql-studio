"""The closed list of BioQL entries Studio may call. Nothing outside it is reachable, whatever a model asks.

An entry: {name, method, path, kind, required, optional, timeout, approval, enabled, why}.
- `path` comes only from here, never from the caller; `{param}` segments are filled from the call's arguments and
  must look like an id.
- `kind`: "status" (reads state), "local" (cheap computation on this Mac), "compute" (a pipeline: GPU, cloud, long),
  "hardware" (may reach a real QPU). `approval: true` and "hardware" no longer refuse anything (2026-11-20): they are
  now a LABEL OF COST, carried onto the tool's description and onto the recorded line. The gates that stop a spend
  live in BioQL's own server environment.
- The owner's file (HOME/allowlist.json, 0600) is merged over DEFAULTS; `allow` copies an entry's schema from BioQL's
  own live registry (/api/tools/registry) so parameters are BioQL's, not guessed.

Some routes are never callable, whatever the file says (DENY): they execute code, open shells, hold credentials or
destroy state.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import home, write_private

KINDS = ("status", "local", "compute", "hardware")
METHODS = ("GET", "POST")
# How the call reaches BioQL. "direct": POST straight at the entry's own path. "submit": POST to BioQL's own async
# door, /api/pipeline/submit, with {endpoint: <this entry's path>, params: <the arguments>}, then follow the job —
# the path still comes from the allowlist, never from the caller. BioQL's own UI uses the second for every long
# pipeline, because a held connection drops on a proxy timeout and the result is lost
# (src/frontend/src/components/chat/ChatInterface.tsx).
VIA = ("direct", "submit")
SUBMIT_PATH = "/api/pipeline/submit"

DENY = re.compile(r"(^|/)(ssh|scripts|exec|execute|repl|shell|terminal|auth|admin|login|secrets?|keys?|"
                  r"cancel_queued|delete|remove|purge|reset|kill|restart|shutdown|deploy|upload)(/|$|_)", re.I)

# BioQL's own language models. The new system has ONE brain — Claude Code — so these are not reached at all: not
# refused as dangerous, simply replaced. Routing a question through BioQL's default model would put a second,
# unaccountable model in a system whose whole claim is that every call is on a signed record.
MODELS = re.compile(r"(^|/)(localllm|llm|openrouter|marie|deepseek|ollama|vmlx|chat/completions|v1/models|"
                    r"embeddings|audio/(speech|transcriptions))(/|$|_)", re.I)

DEFAULTS: dict[str, dict] = {
    "backend_health": {"schema_known": True, "method": "GET", "path": "/health", "kind": "status", "timeout": 15,
                       "why": "Whether BioQL's backend answers, and which services it sees."},
    "tools_registry": {"schema_known": True, "method": "GET", "path": "/api/tools/registry", "kind": "status", "timeout": 30,
                       "why": "BioQL's own typed list of pipelines (endpoint, required parameters, cost tier)."},
    "live_status": {"schema_known": True, "method": "GET", "path": "/api/system/live_status", "kind": "status", "timeout": 30,
                    "why": "Pipelines running now and recent sessions."},
    "pipeline_job": {"schema_known": True, "method": "GET", "path": "/api/pipeline/job/{job_id}", "kind": "status", "timeout": 30,
                     "required": ["job_id"], "absent_on_404": True,
                     "why": "The state of one pipeline job (a large result is not in here; see pipeline_job_result)."},
    "pipeline_job_result": {"schema_known": True, "method": "GET", "path": "/api/pipeline/job/{job_id}/result", "kind": "status",
                            "timeout": 300, "required": ["job_id"], "absent_on_404": True,
                            "why": "The finished result of one pipeline job, which BioQL keeps off the status body."},
    "motifs_list": {"schema_known": True, "method": "GET", "path": "/api/motifs/list", "kind": "status", "timeout": 30,
                    "why": "The chemical motif catalogue (SMARTS)."},
    "smiles_to_3d": {"schema_known": True, "method": "POST", "path": "/api/smiles-to-3d", "kind": "local", "timeout": 120,
                     "required": ["smiles"], "why": "A 3D structure (SDF) for a SMILES, computed locally."},
    "render_2d": {"schema_known": True, "method": "POST", "path": "/api/render/2d", "kind": "local", "timeout": 120,
                  "required": ["smiles"], "optional": ["width", "height", "format"],
                  "why": "A 2D depiction of a molecule."},
}

# A path segment Studio will put in a URL. It must contain a letter or a digit and may not be all dots: `..` matched
# the old pattern, and `urllib.parse.quote` does not escape a dot even with `safe=""`, so `pipeline_job(job_id="..")`
# built `/api/pipeline/job/..` — which the server normalises to a DIFFERENT route than the allowlist named. The whole
# point of the closed list is that the caller cannot choose the route.
ID = re.compile(r"^(?=.*[A-Za-z0-9])[A-Za-z0-9_.-]{1,128}$")
PARAM = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def _norm(name: str, e: dict) -> dict:
    # `schema_known` is the difference between "this entry takes nothing" and "BioQL never told us what it takes".
    # A route discovered from the engine's source or from its window declares no parameter list, and reading that
    # emptiness as "takes nothing" made the adapter refuse EVERY real argument to 128 of the first 193 entries —
    # they were listed and unusable, which from the outside looks exactly like a missing tool.
    known = e.get("schema_known")
    if known is None:
        known = bool(e.get("required") or e.get("optional")) or bool(PARAM.findall(str(e.get("path", ""))))
    out = {"name": name, "method": str(e.get("method", "GET")).upper(), "path": str(e.get("path", "")),
           "schema_known": bool(known),
           "via": e.get("via", "direct"), "kind": e.get("kind", "compute"), "required": list(e.get("required") or []),
           "optional": list(e.get("optional") or []), "timeout": float(e.get("timeout") or 43200),
           "approval": bool(e.get("approval", False)), "enabled": bool(e.get("enabled", True)),
           "absent_on_404": bool(e.get("absent_on_404", False)), "why": str(e.get("why", "")),
           # Los parametros que el handler de BioQL LEE, sacados de su codigo (`tools/discover_params.py`).
           # Campo propio a proposito: NO entra en `required` ni en `optional`, que son los que deciden a quien
           # se le rechaza una llamada. Una lectura en el codigo no es una declaracion -- marcar obligatorio algo
           # que no lo es romperia llamadas que hoy funcionan -- asi que esto solo sirve para DECIR en la
           # descripcion que nombres espera la ruta, que era lo que Claude tenia que adivinar en 386 de 502.
           "params_seen": sorted(set(e.get("params_seen") or [])),
           "from_registry": e.get("from_registry")}
    if out["kind"] in ("compute", "hardware"):
        # No es un defecto que el fichero pueda sobreescribir: el registro de BioQL nunca dice si una pipeline
        # llega a un QPU, asi que todo lo mas pesado que un calculo local queda MARCADO COMO CARO. Desde
        # 2026-11-20 esa marca no rechaza nada -- es la etiqueta de coste que viaja a la descripcion y a la linea
        # del registro. Para abaratar una, el dueño le cambia el KIND a "local".
        out["approval"] = True
    return out


def problems(e: dict) -> list[str]:
    """Why an entry cannot be used at all (an empty list: it is well-formed)."""
    why = []
    if not re.match(r"^[a-z][a-z0-9_]{0,63}$", e["name"]):
        why.append("bad name")
    if e["method"] not in METHODS:
        why.append(f"method {e['method']} not allowed")
    if e["kind"] not in KINDS:
        why.append(f"unknown kind {e['kind']}")
    if e["via"] not in VIA:
        why.append(f"unknown via {e['via']}")
    if e["via"] == "submit" and e["method"] != "POST":
        why.append("via=submit is a POST")
    p = e["path"]
    if not p.startswith("/") or "//" in p or ".." in p or "?" in p or "#" in p:
        why.append("bad path")
    if DENY.search(p):
        why.append("route is never callable from Studio (runs code, holds credentials or destroys state)")
    if MODELS.search(p):
        why.append("that is one of BioQL's own language models; this system has one brain, Claude Code")
    return why


def path_file() -> Path:
    return home() / "allowlist.json"


def load(file: Path | None = None) -> dict[str, dict]:
    """DEFAULTS, then the owner's file over them. An unreadable file is an error, never a silent empty list."""
    f = file or path_file()
    entries = {k: _norm(k, v) for k, v in DEFAULTS.items()}
    if f.exists():
        raw = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"{f}: expected an object of entries")
        for k, v in raw.items():
            if not isinstance(v, dict):
                raise ValueError(f"{f}: entry {k!r} is not an object")
            base = DEFAULTS.get(k, {})
            # A built-in entry's schema was written by hand and IS known, even when it takes nothing — otherwise
            # `tools_registry` would start accepting arguments it has no use for.
            merged = {**base, **v}
            # A file's EMPTY parameter list never erases a default's. A discovery tool that only reads path
            # parameters writes `"required": []`, and merging that over a known schema silently turned
            # `render_2d(smiles=…)` into a route that takes nothing — the entry stayed listed and stopped working.
            for key in ("required", "optional"):
                if base.get(key) and not v.get(key):
                    merged[key] = base[key]
            # A built-in was written by hand; a discovery tool only GUESSES these from a route's Allow header.
            # One such guess turned `smiles_to_3d` from POST/local into GET/status, and a GET carries no body, so
            # the engine answered "No SMILES provided" to a call that carried the SMILES.
            if base and v.get("from_registry", {}).get("route_from") in ("bioql-ui", "bioql-backend"):
                for key in ("method", "kind", "via", "approval", "absent_on_404", "timeout", "why"):
                    if key in base:
                        merged[key] = base[key]
            entries[k] = _norm(k, merged)
    # The same door under two names must not have two locks. 34 routes are in this list twice — a built-in written
    # by hand and an entry a discovery sweep wrote for the same `METHOD path` — and wherever the two gates
    # disagreed the loose name was a way round the strict one: `chem_amp_bms` reached POST /api/chem/amp_bms with
    # no approval while `amp_bms_md` asked a person for that very route. The strictest gate a route carries under
    # ANY of its names applies to all of them.
    gate: dict[tuple[str, str], bool] = {}
    for e in entries.values():
        key = (e["method"], e["path"])
        gate[key] = gate.get(key, False) or bool(e["approval"])
    for e in entries.values():
        if gate[(e["method"], e["path"])]:
            e["approval"] = True
    return entries


def save_entry(name: str, entry: dict, file: Path | None = None) -> dict:
    f = file or path_file()
    e = _norm(name, entry)
    bad = problems(e)
    if bad:
        raise ValueError(f"{name}: " + "; ".join(bad))
    raw = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    raw[name] = {k: v for k, v in e.items() if k != "name"}
    write_private(f, json.dumps(raw, indent=2, ensure_ascii=False) + "\n")
    return e


def from_registry(tool: dict, kind: str | None = None) -> dict:
    """An entry built from one tool of BioQL's /api/tools/registry. Only HTTP tools with an endpoint qualify."""
    if tool.get("kind") != "http" or not tool.get("endpoint"):
        raise ValueError(f"{tool.get('name')}: not an HTTP entry with an endpoint in BioQL's registry")
    s = tool.get("schema") or {}
    tier = str(s.get("cost_tier") or "").lower()
    # BioQL's own tiers are only "high" and "medium" (63 of 63 pipelines, checked 2026-11-19): nothing in the registry
    # says whether a pipeline can reach a real QPU. So no entry is auto-classed "hardware" from the tier, and every
    # `compute` entry is MARKED as costing real compute. Since 2026-11-20 that mark refuses nothing -- it is what the
    # description and the recorded line carry, so a reader of either can tell a simulator from a chip.
    k = kind or "compute"
    return {"method": "POST", "path": tool["endpoint"], "kind": k,
            "required": list(s.get("required") or []), "optional": list(s.get("optional") or []),
            "approval": k in ("compute", "hardware") or bool(s.get("confirmation_required")), "timeout": 43200,
            "why": (s.get("notes") or "")[:300], "from_registry": meta_of(tool)}


# What BioQL says ABOUT a tool, as opposed to how to call it. This used to be thrown away and it is the most
# useful thing in the registry: with 502 entries in front of it, a model picks by these words.
#
# `forbidden_inference` is the one that matters most and it is not a nicety — it is BioQL stating, per pipeline,
# which arguments a model MUST NOT invent. `peptide_discovery` forbids inferring `target` and `indication`: those
# are the two that decide what gets designed and for what disease. Studio was dropping that rule on the floor.
META_KEYS = ("cost_tier", "modality", "intent_terms", "forbidden_inference", "mutually_exclusive",
             "confirmation_required")


def meta_of(tool: dict) -> dict:
    s = tool.get("schema") or {}
    out = {"name": tool.get("name"), "version": s.get("version")}
    for key in META_KEYS:
        v = s.get(key)
        if v not in (None, "", [], {}):
            out[key] = [str(x)[:60] for x in v][:12] if isinstance(v, list) else v
    if isinstance(out.get("cost_tier"), str):
        out["cost_tier"] = out["cost_tier"].lower()
    return out


def refresh(reg_tools: list[dict]) -> dict:
    """Backfill what BioQL says about entries that already exist, from its own registry.

    The 498 entries on this Mac were written before `meta_of` kept any of it, so every one of them had lost its
    intent terms, its modality and — the one that matters — its `forbidden_inference`. This puts that back.

    What it will NOT touch, ever: `path`, `kind`, `approval`, `enabled`, `method`, `via`, `timeout`. Those are the
    owner's decisions about what Studio may reach and what needs a person, and a refresh from BioQL's registry is
    not a vote on them. `required`/`optional` are filled only when the entry has NONE — BioQL is the source for
    them, but an entry the owner has already shaped is not overwritten behind their back.
    """
    by_name: dict[str, dict] = {}
    by_path: dict[str, dict] = {}
    for t in reg_tools:
        if t.get("name"):
            by_name[str(t["name"])] = t
        if t.get("endpoint"):
            by_path[str(t["endpoint"])] = t
    f = path_file()
    own = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    changed, filled, missed = 0, 0, 0
    for name, e in own.items():
        t = by_name.get(str((e.get("from_registry") or {}).get("name") or name)) or by_name.get(name) \
            or by_path.get(str(e.get("path") or ""))
        if t is None:
            missed += 1
            continue
        meta = meta_of(t)
        if meta != (e.get("from_registry") or {}):
            e["from_registry"] = {**(e.get("from_registry") or {}), **meta}
            changed += 1
        s = t.get("schema") or {}
        if not e.get("required") and not e.get("optional") and (s.get("required") or s.get("optional")):
            e["required"] = list(s.get("required") or [])
            e["optional"] = list(s.get("optional") or [])
            filled += 1
    write_private(f, json.dumps(own, indent=2, ensure_ascii=False) + "\n")
    return {"entries": len(own), "metadata_updated": changed, "parameters_filled": filled,
            "not_in_registry": missed,
            "note": "paths, kinds, approval flags and enabled stay exactly as they were: this reads BioQL, it "
                    "does not re-decide what Studio may reach."}
