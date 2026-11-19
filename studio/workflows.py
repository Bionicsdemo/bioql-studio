"""The catalogue of what Studio can reach, and the multi-step sequences a drug-discovery person actually runs.

193 allowlist entries is not a usable list, for a person or for a model. So this module groups them the way the work
is organised — the area, what it does, what it takes, whether it needs a person, and whether BioQL itself confirmed
the route — and builds a handful of named sequences on top.

What it owns:
  catalogue()   every entry, grouped by area, nothing dropped: a disabled entry is listed as excluded with its
                reason, and an enabled-but-malformed one stays in its group flagged with `problems`.
  search(q)     an entry by name, area, parameter or purpose, matched on WHOLE TOKENS. Never a substring: `"kd" in
                path` also matches `score_breakdown`, which once turned a 60% audit rate into 94% (CLAUDE.md).
  WORKFLOWS     named sequences as DATA. This module does not run anything; `studio/campaign.py` does.
  plan()        one workflow plus seed arguments resolved into the exact plan `campaign.plan` validates
                ({title, criteria, runs:[{tool, args, why}]} and nothing else).
  cost()        per step: a read, a local computation, or something that needs a person — and never money or minutes.

Three things this module refuses to do:
  - guess an argument. A step whose argument can only come from an earlier step's result keeps the REFERENCE
    (`$1.value.result...`) as its value and is marked unresolved; it is not filled with a plausible molecule.
  - trim a plan. A step that `Adapter.check` refuses makes the whole plan REJECTED, naming the entry and the reason.
  - price a run. BioQL's registry says only `high` or `medium` and never says whether a route reaches a QPU, so the
    honest answer about hardware is "a person decides", quoted as such, not a number.
"""
from __future__ import annotations

import re
from typing import Any

from . import allowlist as al
from .adapter import Adapter, Refused, wants_hardware

# The areas, in the order a person works rather than the order the URLs fall. Same names (and same patterns) as
# tools/discover_surface.py, which records an area on the entries it discovers from BioQL's own window. The table is
# repeated here because only those entries carry one: the 63 registry pipelines and Studio's DEFAULTS do not, and an
# entry with no recorded area still has to land in a group.
AREAS = [
    ("health", r"^/(api/(health|system/|v1/quantum/health|quantum/health|protenix/health|targetradar/health)|health$)"),
    ("jobs", r"^/api/(pipeline/job|pipeline/submit|recovery/|scripts/(status|output))"),
    ("chem", r"^/api/(smiles-to-3d|render/|rdkit|motifs/|chem/|structure/|viz/)"),
    ("targets", r"^/api/(targets|targetradar/|motifs)"),
    ("screening", r"^/api/(screening/|docking/|vina|sar/)"),
    ("notebook", r"^/api/(eln/|reports?/|candidates)"),
    ("quantum", r"^/api/(quantum/|v11/|complexa/|protenix/)"),
    ("pipeline", r"^/api/(pipeline/|casestudy/|aging/|bionics/|genomics/)"),
    ("tools", r"^/api/(tools/|bioql|validation/|typesafe/)"),
]
AREA_ORDER = [a for a, _ in AREAS] + ["other"]

READ, LOCAL, PERSON, UNKNOWN_TOOL = "read", "local computation", "needs a person", "not in the allowlist"
# Hardware, in words, never as a probability. BioQL's registry has no field that says whether a route reaches a QPU.
HW_NO = "no: a read, or a computation classified local in the allowlist"
HW_PERSON = "a person decides: nothing in BioQL's registry says whether this route reaches a QPU"
HW_DECLARED = "declared `hardware` in the allowlist: a person must approve it"
HW_ASKED = "the arguments ask for real hardware: a person must approve it"
NOT_PRICED = "not estimated: Studio does not price or time a BioQL run, and BioQL's registry does not either"

# `Adapter.check` wants an approver for every `compute` entry, and a plan makes no call at all — so the gate is
# satisfied here by a sentinel and nowhere else, exactly as `campaign._PLAN_CHECK` does it. `cost()` still reports
# every one of those steps as `needs a person`.
PLAN_CHECK = "workflow plan validation only, no call is made"

SEED = "$seed."
REF = re.compile(r"^\$(\d+)\.(.+)$")
TOKEN = re.compile(r"[a-z0-9]+")
# The allowlist's `why` for a discovered route starts with its area in brackets; the purpose line drops it.
AREA_TAG = re.compile(r"^\[[a-z]+\]\s*")
# What `tools/discover_surface.py` writes as the `why` of EVERY route it finds. 128 of the 193 entries say exactly
# this, so it is not a purpose: it is the absence of one, and it is flagged rather than repeated as if it told you
# something. It is also kept out of search, where it would match a third of the list on the word "window".
BOILERPLATE = "used by BioQL's own window"
# A step whose entry records no parameters. Empty `required`/`optional` on a route discovered from BioQL's window
# means its body schema was never recorded, NOT that the route takes nothing (tools/discover_surface.py reads only
# the path parameters). So such a step carries no body, and says so.
NO_PARAMS = ("the allowlist records no parameters for this entry — unknown, not none — so this run carries no "
             "arguments; what BioQL's own window sends here was never recorded")
REF_UNVERIFIED = "the path into the earlier result is not verified against a real BioQL body"


class PlanRejected(ValueError):
    """Studio will not build this plan. The message names the step and the reason; nothing is trimmed to fit."""

    def __init__(self, why: str, workflow: str = "") -> None:
        super().__init__(why)
        self.workflow = workflow


# ---------------------------------------------------------------------------- the catalogue
def _tokens(*parts: Any) -> set[str]:
    return {t for p in parts if p for t in TOKEN.findall(str(p).lower())}


def area_of(entry: dict) -> tuple[str, str]:
    """(area, where it came from). A recorded area always wins; otherwise it is classified from the path, which is a
    guess and says so, so a reader never mistakes it for something BioQL stated."""
    rec = (entry.get("from_registry") or {}).get("area")
    if isinstance(rec, str) and rec in AREA_ORDER:
        return rec, "recorded at discovery"
    path = entry.get("path") or ""
    for name, pat in AREAS:
        if re.match(pat, path):
            return name, "classified from the path by Studio"
    return "other", "classified from the path by Studio"


def _purpose(e: dict) -> tuple[str, bool]:
    """(one line saying what the entry is for, whether the allowlist really records one). Never invented: with
    nothing recorded — or only discovery's boilerplate — it says so."""
    why = AREA_TAG.sub("", " ".join(str(e.get("why") or "").split())).strip()
    if why and why != BOILERPLATE:
        return why[:200], True
    tail = f" (BioQL's window calls it; {BOILERPLATE})" if why == BOILERPLATE else ""
    return f"no purpose recorded in the allowlist ({e['method']} {e['path']}){tail}", False


def family_of(path: str) -> str:
    """What the route is about, one level below its area — the second level, because `quantum` alone is 79 entries.

    Where BioQL's route HAS a directory (`/api/quantum/docking/boltz2`, 46 of the 193) that directory is the group:
    BioQL's own grouping, read rather than invented. For a two-segment route the leading word of the leaf is used
    (`/api/quantum/peptide_design` -> `quantum/peptide`), and that IS a guess at BioQL's naming — which is why every
    group carries `group_source` and a lone sub-group is folded back into its head.
    """
    parts = [p for p in path.split("/") if p and p != "api"]
    if not parts:
        return path or "other"
    if len(parts) > 2:
        return f"{parts[0]}/{parts[1]}"
    head, leaf = parts[0], parts[-1]
    bits = re.split(r"[_-]", leaf)
    return f"{head}/{bits[0]}" if len(parts) > 1 and len(bits) > 1 and bits[0] != head else head


def _hardware(e: dict) -> str:
    if e["kind"] == "hardware":
        return HW_DECLARED
    return HW_NO if e["kind"] in ("status", "local") else HW_PERSON


def _klass(e: dict) -> str:
    return {"status": READ, "local": LOCAL}.get(e["kind"], PERSON) if not e["approval"] else PERSON


def card(name: str, e: dict) -> dict:
    """One catalogue line for one entry: what it does, what it takes, who has to say yes, where the route came from."""
    area, src = area_of(e)
    reg = e.get("from_registry") or {}
    confirmed = reg.get("route_confirmed_by_bioql")
    purpose, recorded = _purpose(e)
    return {"name": name, "area": area, "area_source": src, "group": family_of(e["path"]),
            "purpose": purpose, "purpose_recorded": recorded,
            "kind": e["kind"], "method": e["method"], "path": e["path"], "via": e["via"],
            "required": list(e["required"]), "optional": list(e["optional"]),
            # Empty is UNKNOWN here, not "takes nothing": see NO_PARAMS.
            "parameters_recorded": bool(e["required"] or e["optional"]),
            "needs_person": bool(e["approval"] or e["kind"] == "hardware"),
            "class": _klass(e), "could_reach_real_hardware": _hardware(e),
            # True when BioQL itself answered for this route; None when nobody asked. Never False by absence.
            "route_confirmed_by_bioql": True if confirmed is True else None,
            "route_from": reg.get("route_from") or ("bioql registry" if reg.get("name") else None),
            "registry": {"name": reg.get("name"), "cost_tier": reg.get("cost_tier"),
                         "version": reg.get("version")} if reg.get("name") else None,
            "problems": al.problems(e), "callable": not al.problems(e)}


def catalogue(entries: dict[str, dict] | None = None) -> dict:
    """Every allowlist entry, grouped by area and then by what it does. Nothing is lost: each enabled entry appears
    exactly once, in exactly one group of one area, and a disabled one is listed under `excluded` with its reason.

    Two levels because one is not usable: `quantum` alone holds 79 of the 193 entries. The second level is Studio's
    own reading of BioQL's route naming (see `family_of`), labelled as such in `group_source`, while the area itself
    is recorded by discovery for the routes that came from BioQL's window.
    """
    ents = al.load() if entries is None else entries
    cards: list[dict] = []
    excluded: list[dict] = []
    for name, e in sorted(ents.items()):
        if not e["enabled"]:
            excluded.append({"name": name, "area": area_of(e)[0], "reason": "not enabled in the allowlist"})
            continue
        cards.append(card(name, e))
    # One route reached by two names is one route: Claude's tool list would otherwise offer it twice, and the second
    # copy is usually the one whose parameter schema was never recorded.
    by_route: dict[tuple, list[str]] = {}
    for c in cards:
        by_route.setdefault((c["method"], c["path"]), []).append(c["name"])
    rules = {k: {(x["kind"], x["needs_person"]) for x in cards if (x["method"], x["path"]) == k} for k in by_route}
    for c in cards:
        same = [n for n in by_route[(c["method"], c["path"])] if n != c["name"]]
        c["same_route_as"] = same
        # Two names for one route that disagree about approval: calling it by one name asks a person and by the other
        # does not, so the gate depends on the spelling. `chem_amp_bms` (local) and `amp_bms_md` (compute) are one
        # POST /api/chem/amp_bms.
        c["route_rules_disagree"] = bool(same) and len(rules[(c["method"], c["path"])]) > 1
    areas = []
    for a in AREA_ORDER:
        mine = [c for c in cards if c["area"] == a]
        if not mine:
            continue
        # A group of one is not a group: `quantum/flexible` with a single entry in it is noise, so a lone
        # sub-group falls back to its head (`quantum`). Grouping exists to make 79 entries readable; splitting them
        # into 41 slivers is the same unusable flat list with more lines.
        tally: dict[str, int] = {}
        for c in mine:
            tally[c["group"]] = tally.get(c["group"], 0) + 1
        for c in mine:
            if tally[c["group"]] == 1 and "/" in c["group"]:
                c["group"] = c["group"].split("/")[0]
        gs: dict[str, list[dict]] = {}
        for c in mine:
            gs.setdefault(c["group"], []).append(c)
        areas.append({"area": a, "count": len(mine),
                      "needs_person": sum(1 for c in mine if c["needs_person"]),
                      "reads": sum(1 for c in mine if c["class"] == READ),
                      "not_callable": sum(1 for c in mine if not c["callable"]),
                      "group_source": "derived from the route path by Studio, not stated by BioQL",
                      "groups": [{"group": g, "count": len(gs[g]), "entries": gs[g]} for g in sorted(gs)]})
    return {"total": len(cards), "counts": {g["area"]: g["count"] for g in areas},
            "group_counts": {g["area"]: {x["group"]: x["count"] for x in g["groups"]} for g in areas},
            "needs_person": sum(1 for c in cards if c["needs_person"]),
            "reads": sum(1 for c in cards if c["class"] == READ),
            "local": sum(1 for c in cards if c["class"] == LOCAL),
            "not_callable": sum(1 for c in cards if not c["callable"]),
            "parameters_not_recorded": sum(1 for c in cards if not c["parameters_recorded"]),
            "purpose_not_recorded": sum(1 for c in cards if not c["purpose_recorded"]),
            "duplicate_routes": {f"{m} {p}": sorted(v) for (m, p), v in sorted(by_route.items()) if len(v) > 1},
            "routes_whose_rules_disagree": {f"{m} {p}": sorted(v) for (m, p), v in sorted(by_route.items())
                                            if len(v) > 1 and len(rules[(m, p)]) > 1},
            "areas": areas, "area_order": list(AREA_ORDER), "excluded": excluded}


def cards(cat: dict) -> list[dict]:
    """Every card of a catalogue, flat — for a caller that wants the list rather than the grouping."""
    return [c for a in cat["areas"] for g in a["groups"] for c in g["entries"]]


# ---------------------------------------------------------------------------- search
def search(q: str, entries: dict[str, dict] | None = None, limit: int | None = None) -> list[dict]:
    """Entries matching `q` by name, area, parameter, kind, path or purpose — matched on whole tokens only.

    Substring matching is the bug this signature exists to avoid: `"kd" in "score_breakdown"` is True, and an audit
    once read 94% because of it. A query token matches a field token when the two are EQUAL.

    Every match is returned unless the caller asks for a limit: a default cap would have dropped 33 of the 83
    entries that match "quantum" without saying so.
    """
    ents = al.load() if entries is None else entries
    want = _tokens(q)
    raw = str(q or "").strip().lower()
    if not want:
        return []
    out = []
    for name, e in ents.items():
        if not e["enabled"]:
            continue
        area, _ = area_of(e)
        purpose, recorded = _purpose(e)
        fields = {"name": _tokens(name), "area": {area}, "kind": {e["kind"]},
                  "parameter": _tokens(*e["required"], *e["optional"]),
                  # Only a purpose the allowlist really records: discovery's boilerplate is on 128 entries, and
                  # matching it would answer "window" with a third of the list.
                  "path": _tokens(e["path"]), "purpose": _tokens(purpose if recorded else "")}
        weight = {"name": 100, "area": 40, "parameter": 30, "kind": 20, "path": 15, "purpose": 10}
        score, hit = 0, []
        for field, toks in fields.items():
            shared = want & toks
            if shared:
                score += weight[field] * len(shared)
                hit.append(field)
        if raw == name:
            score += 1000                      # the exact name first, whatever else matches
        elif want and want <= fields["name"]:
            score += 400                       # every query token is in the name
        if not score:
            continue
        # No `group` here on purpose: the catalogue folds a lone sub-group into its head, so a group printed from
        # one entry alone would not be the group the catalogue shows.
        out.append({"name": name, "area": area, "kind": e["kind"], "class": _klass(e), "needs_person": bool(e["approval"] or e["kind"] == "hardware"),
                    "purpose": purpose, "purpose_recorded": recorded,
                    "matched_on": sorted(hit), "score": score})
    out.sort(key=lambda r: (-r["score"], r["name"]))
    return out if limit is None else out[:limit]


# ---------------------------------------------------------------------------- the workflows (data, not code)
# Each step: {tool, args_from, why} and optionally `args` for a literal the workflow itself fixes. An `args_from`
# source is either `$seed.<key>` (the person's own input) or `$<step>.<path>` (an earlier step's result, left
# unresolved and marked — `studio/campaign.py` is what runs a plan, and only a person confirms one).
WORKFLOWS: dict[str, dict] = {
    "molecule at a glance": {
        "title": "One molecule, seen three ways",
        "question": "What is this molecule, and is there any reason to stop now?",
        "criteria": ["the molecule renders and has a 3D structure",
                     "an ADMET profile is reported with the provenance BioQL stated"],
        "seed": ["smiles"],
        "steps": [
            {"tool": "render_2d", "args_from": {"smiles": "$seed.smiles"},
             "why": "see what the molecule actually is before spending anything on it"},
            {"tool": "smiles_to_3d", "args_from": {"smiles": "$seed.smiles"},
             "why": "a 3D structure: what docking and any viewer need"},
            {"tool": "admet_standalone", "args_from": {"smiles": "$seed.smiles"},
             "why": "absorption, metabolism and the early tox flags, before design work continues"},
        ],
    },
    "target to candidate": {
        "title": "From a target to one candidate with numbers on it",
        "question": "Starting from this target, is there a candidate worth docking and profiling?",
        "criteria": ["a candidate is designed for the target, with its backend stated",
                     "that candidate is docked against the target's structure",
                     "that candidate has an ADMET profile"],
        # Two seed keys, not one: the designer takes the target by NAME and the docking route takes a STRUCTURE or a
        # sequence. Feeding the name into both would be a guess dressed as a resolution — `Adapter.check` looks at
        # the key, not at whether the value is the right kind of thing.
        "seed": ["target", "target_structure_or_sequence"],
        "steps": [
            {"tool": "target_quantum_design", "args_from": {"target": "$seed.target"},
             "why": "pocket, then a generator conditioned on it: BioQL's own target design route"},
            {"tool": "quantum_docking_boltz2",
             "args_from": {"target_structure_or_sequence": "$seed.target_structure_or_sequence",
                           "ligand_smiles": "$1.value.result.smiles"},
             "why": "dock what step 1 designed, not a molecule anyone typed in"},
            {"tool": "admet_standalone", "args_from": {"smiles": "$1.value.result.smiles"},
             "why": "a candidate with no ADMET is not a candidate"},
        ],
    },
    "screen a library": {
        "title": "A library, narrowed and then explained",
        "question": "Which molecules of this library survive docking, and what does the series say?",
        "criteria": ["a library is screened and the hits are named",
                     "the hits are docked",
                     "the series is explained by matched pairs rather than by one number"],
        "seed": [],
        "steps": [
            {"tool": "screening_chembl", "args_from": {},
             "why": "the library pass BioQL's screening pane runs"},
            {"tool": "docking_classical", "args_from": {},
             "why": "dock the survivors with the classical arm, which is the one that is hard to beat"},
            {"tool": "sar_mmp", "args_from": {},
             "why": "matched molecular pairs: what actually changed activity, not a ranking"},
        ],
    },
    "due diligence pack": {
        "title": "What the engine says it is, and what it has proved",
        "question": "What can be claimed about this target or product, with the engine's own record behind it?",
        "criteria": ["BioQL's own registry of pipelines is recorded as it stood",
                     "the validation metrics BioQL publishes are recorded",
                     "a diligence analysis is produced for the target or product",
                     "a dossier is exported from what was recorded"],
        "seed": ["target_or_product", "jurisdiction"],
        "steps": [
            {"tool": "tools_registry", "args_from": {},
             "why": "what the engine says it can do, at the moment of the pack: the baseline for every claim in it"},
            {"tool": "validation_metrics", "args_from": {},
             "why": "the metrics BioQL itself publishes, read rather than restated"},
            {"tool": "due_diligence_report",
             "args_from": {"target_or_product": "$seed.target_or_product", "jurisdiction": "$seed.jurisdiction"},
             "why": "the diligence analysis itself, on BioQL's own route"},
            {"tool": "reports_generate_dossier", "args_from": {},
             "why": "export the dossier from what was recorded above"},
        ],
    },
}


def workflows(entries: dict[str, dict] | None = None) -> list[dict]:
    """Every workflow against THIS allowlist: which are plannable, and for the rest exactly which entry is missing.

    A workflow whose tool is not in the list is reported here with the reason. It is never quietly dropped and its
    steps are never quietly shortened: `plan()` on it raises.
    """
    ents = al.load() if entries is None else entries
    out = []
    for name, wf in WORKFLOWS.items():
        missing, argued = [], []
        for i, s in enumerate(wf["steps"], 1):
            e = ents.get(s["tool"])
            if e is None:
                missing.append({"step": i, "tool": s["tool"], "reason": "not in Studio's allowlist"})
                continue
            if not e["enabled"]:
                missing.append({"step": i, "tool": s["tool"], "reason": "in the allowlist but not enabled"})
                continue
            if al.problems(e):
                missing.append({"step": i, "tool": s["tool"], "reason": "; ".join(al.problems(e))})
                continue
            # The entry is there, but its recorded schema may not accept what this step sends — the case the owner's
            # own list produces for `render_2d`, whose `smiles` was lost at discovery. Reported here so the pane does
            # not offer a workflow `plan()` will reject.
            keys = set(s["args_from"]) | set(s.get("args") or {})
            allowed = set(e["required"]) | set(e["optional"]) | set(al.PARAM.findall(e["path"]))
            extra = sorted(keys - allowed)
            short = sorted(k for k in e["required"] if k not in keys)
            if extra:
                argued.append({"step": i, "tool": s["tool"],
                               "reason": f"does not take {extra} (it takes {sorted(allowed) or 'nothing'})"})
            if short:
                argued.append({"step": i, "tool": s["tool"], "reason": f"needs {short}, which this step does not set"})
        bad = missing + argued
        out.append({"name": name, "title": wf["title"], "question": wf["question"], "criteria": list(wf["criteria"]),
                    "seed": list(wf["seed"]), "steps": len(wf["steps"]),
                    "tools": [s["tool"] for s in wf["steps"]],
                    "needs_seed": list(wf["seed"]), "missing": missing, "argument_problems": argued,
                    "usable": not bad,
                    "skipped_because": "; ".join(f"step {m['step']} {m['tool']}: {m['reason']}" for m in bad)})
    return out


# ---------------------------------------------------------------------------- plan
def plan(workflow_name: str, seed_args: dict | None = None, adapter: Adapter | None = None,
         entries: dict[str, dict] | None = None) -> dict:
    """One workflow, resolved into the plan `campaign.plan` validates: {title, criteria, runs:[{tool, args, why}]}.

    Exactly those keys and no others, because `campaign._validate` rejects a plan that carries a field it does not
    read — which is why an unresolved reference is marked INSIDE the step (the argument keeps the `$1...` reference
    and the `why` says so) instead of in a key of its own. `cost()` reads them back out.

    Every step goes through `Adapter.check`. A step the adapter refuses — an unknown tool, a missing argument, an
    argument the entry does not take — rejects the WHOLE plan, naming the step and the reason.
    """
    wf = WORKFLOWS.get(workflow_name)
    if wf is None:
        raise PlanRejected(f"no workflow named {workflow_name!r} (there are: {sorted(WORKFLOWS)})", workflow_name)
    a = adapter or Adapter(entries=entries)
    seed = dict(seed_args or {})
    wanted = {src[len(SEED):] for s in wf["steps"] for src in s["args_from"].values() if src.startswith(SEED)}
    missing = sorted(k for k in wanted if seed.get(k) in (None, ""))
    if missing:
        raise PlanRejected(f"{workflow_name!r} needs {missing} to start; a seed value is never guessed", workflow_name)
    unused = sorted(set(seed) - wanted)
    if unused:
        # A seed key no step consumes is not harmless: it is an argument the caller believed was being used.
        raise PlanRejected(f"{workflow_name!r} does not use {unused} (it starts from {sorted(wanted) or 'nothing'})",
                           workflow_name)

    forged = sorted(k for k, v in seed.items() if isinstance(v, str) and REF.match(v))
    if forged:
        # Otherwise a seed value could impersonate a step reference: `unresolved()` would report it as one, and a
        # reader would believe the campaign engine was going to fill it in.
        raise PlanRejected(f"seed {forged} looks like a step reference; a seed carries a value, not a reference",
                           workflow_name)

    runs = []
    for i, s in enumerate(wf["steps"], 1):
        args, refs = dict(s.get("args") or {}), []
        for k, src in s["args_from"].items():
            if src.startswith(SEED):
                args[k] = seed[src[len(SEED):]]
                continue
            m = REF.match(str(src))
            if not m:
                raise PlanRejected(f"step {i} ({s['tool']}): {src!r} is neither $seed.<key> nor $<step>.<path>",
                                   workflow_name)
            n = int(m.group(1))
            if n >= i:
                # A step cannot read its own result or a later one; resolving that would mean inventing the value.
                raise PlanRejected(f"step {i} ({s['tool']}): {k} points at step {n}, which does not run before it",
                                   workflow_name)
            args[k] = src                      # the REFERENCE stays the value: nothing here guesses a molecule
            refs.append(f"{k}={src}")
        try:
            a.check(s["tool"], args, approved_by=PLAN_CHECK)
        except Refused as why:
            raise PlanRejected(f"step {i} ({s['tool']}): {why}", workflow_name) from None
        why = s["why"]
        if refs:
            why += (f" — unresolved until the earlier step runs: {', '.join(refs)} ({REF_UNVERIFIED})")
        if not args and ents_kind(a, s["tool"]) in ("compute", "hardware"):
            # Only where a body is the question. A GET read takes none, and saying otherwise about `tools_registry`
            # would be a sentence that reads as a finding and is simply false.
            why += f" — {NO_PARAMS}"
        runs.append({"tool": s["tool"], "args": args, "why": why})
    return {"title": wf["title"], "criteria": list(wf["criteria"]), "runs": runs}


def ents_kind(a: Adapter, tool: str) -> str:
    """The kind the adapter's own entry records, so `plan()` never re-reads the allowlist behind its back."""
    e = a.entries.get(tool) or {}
    return str(e.get("kind") or "")


def unresolved(plan_dict: dict) -> list[dict]:
    """Every argument of a plan that is still a reference to an earlier step's result, read back from the plan
    itself — so a reader does not have to trust that `plan()` marked them."""
    out = []
    for n, r in enumerate(plan_dict.get("runs") or [], 1):
        for k, v in (r.get("args") or {}).items():
            m = REF.match(v) if isinstance(v, str) else None
            if m:
                out.append({"step": n, "tool": r.get("tool"), "argument": k, "reference": v,
                            "from_step": int(m.group(1)), "path": m.group(2), "verified": False,
                            "note": REF_UNVERIFIED})
    return out


# ---------------------------------------------------------------------------- cost, in words
def cost(plan_dict: dict, entries: dict[str, dict] | None = None) -> dict:
    """What each step of a plan IS — a read, a local computation, or something that needs a person — and whether it
    could reach real hardware. No money and no minutes: neither Studio nor BioQL's registry knows them, and a number
    there would be a plausible value invented at the point where the honest answer is "a person decides"."""
    ents = al.load() if entries is None else entries
    refs = unresolved(plan_dict)
    steps = []
    for n, r in enumerate(plan_dict.get("runs") or [], 1):
        tool = r.get("tool")
        e = ents.get(tool)
        mine = [x for x in refs if x["step"] == n]
        if e is None:
            steps.append({"n": n, "tool": tool, "class": UNKNOWN_TOOL, "needs_person": True,
                          "could_reach_real_hardware": "unknown: the entry is not in the allowlist",
                          "registry_cost_tier": None, "unresolved": mine,
                          "blocked_until_resolved": bool(mine), "parameters_recorded": None})
            continue
        args = r.get("args") or {}
        hw = HW_ASKED if wants_hardware(args) else _hardware(e)
        reg = e.get("from_registry") or {}
        steps.append({"n": n, "tool": tool, "class": _klass(e),
                      "needs_person": bool(e["approval"] or e["kind"] == "hardware" or wants_hardware(args)),
                      "could_reach_real_hardware": hw,
                      # BioQL's own word, quoted, never turned into a number.
                      "registry_cost_tier": reg.get("cost_tier"),
                      "parameters_recorded": bool(e["required"] or e["optional"]),
                      # A compute route called with no body at all: the entry records no parameters, so the run goes
                      # as BioQL's window never sends it. Named here because it is a reason to look, not a detail.
                      "sent_without_a_body": e["kind"] in ("compute", "hardware") and not args,
                      "unresolved": mine, "blocked_until_resolved": bool(mine)})
    return {"money": NOT_PRICED, "time": NOT_PRICED,
            "steps": steps, "total_steps": len(steps),
            "needs_person": sum(1 for s in steps if s["needs_person"]),
            "reads": sum(1 for s in steps if s["class"] == READ),
            "local": sum(1 for s in steps if s["class"] == LOCAL),
            "unknown_tools": sum(1 for s in steps if s["class"] == UNKNOWN_TOOL),
            "unresolved_total": len(refs),
            "sent_without_a_body": sum(1 for s in steps if s.get("sent_without_a_body")),
            # What this actually checks: that no argument is still a reference. It is NOT a statement that the plan
            # will run correctly — `campaign.execute` submits every run to a thread pool with no ordering and no
            # reference resolution, so a plan with references would POST the literal "$1..." to BioQL, and even a
            # plan without them has its steps run in no particular order.
            "no_unresolved_references": not refs,
            "order_enforced": False,
            "order_note": "campaign.execute runs the steps in a pool: it neither orders them nor fills a reference",
            "hardware": "a person decides: no field of BioQL's registry says whether a route reaches a QPU"}
