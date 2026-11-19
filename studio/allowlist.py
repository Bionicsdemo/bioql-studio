"""The closed list of BioQL entries Studio may call. Nothing outside it is reachable, whatever a model asks.

An entry: {name, method, path, kind, required, optional, timeout, approval, enabled, why}.
- `path` comes only from here, never from the caller; `{param}` segments are filled from the call's arguments and
  must look like an id.
- `kind`: "status" (reads state), "local" (cheap computation on this Mac), "compute" (a pipeline: GPU, cloud, long),
  "hardware" (may reach a real QPU). "hardware" entries, and any with `approval: true`, refuse unless the call carries
  a human approval — the model has no way to give one.
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
           "from_registry": e.get("from_registry")}
    if out["kind"] in ("compute", "hardware"):
        # Not a default the file can override: BioQL's registry never says whether a pipeline reaches a QPU, so
        # anything heavier than a local computation asks a person. To make one cheap, the owner changes its KIND to
        # "local" — a deliberate statement about that entry, not a forgotten field.
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
            entries[k] = _norm(k, merged)
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
    # `compute` entry needs a person's approval. The owner relaxes a specific entry by hand, never this function.
    k = kind or "compute"
    return {"method": "POST", "path": tool["endpoint"], "kind": k,
            "required": list(s.get("required") or []), "optional": list(s.get("optional") or []),
            "approval": k in ("compute", "hardware") or bool(s.get("confirmation_required")), "timeout": 43200,
            "why": (s.get("notes") or "")[:300], "from_registry": {"name": tool.get("name"), "version": s.get("version"),
                                                                   "cost_tier": tier or None}}
