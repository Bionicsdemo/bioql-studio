"""Cards: any BioQL result payload turned into a card SPEC — pure data, no HTML and no rendering.

The window draws this spec and Claude reads the same spec, so it has to be honest and complete rather than pretty.

Cards are discovered BY THE SHAPE of the result, never by the tool's name. That is how BioQL's own dispatch works —
`cardRegistry.ts` is a manifest of dot-paths (`stages.1_protein_design.mature_length_aa`, `structure_3d.pdb`,
`raw_predictions`, `categories`), and `ChatMessage.extractPipelineData` sniffs those shapes with `deepFind` for
EVERY pipeline, not for a list of tool names. The consequence is the point: a pipeline nobody has heard of gets a
card for free, and a tool that lies about its name cannot change what is drawn. `tool` here only informs the
subtitle.

What the shapes are, and where each block type comes from:
  facts       a flat object of scalars — BioQL's `summary`, `downloads.session_id`, a receipt's `status`
  table       a list of uniform objects — `results[]`, `candidates[]`, `guides[]`
  molecule2d  `{image_base64, format: "png"}`, the `/api/render/2d` answer, where `format` is the image key's own
              sibling and nothing else counts as a statement of the format
  molecule3d  SDF text: `{mol: "<sdf>"}`, the `/api/smiles-to-3d` answer, and `mol_block`
  radar       `admet.radar_summary`, BioQL's OWN eight scores on 0..1 — the axes and the clamp are copied from
              `ADMETRadarChart.tsx`'s FIRST path, the one its own comment calls "quantum-computed, not heuristic".
              Its SECOND path is not copied: when `radar_summary` is missing, that component computes the shape
              itself — a category with no numbers becomes 0.5, a value above 1 is "assumed" to be a percentage, and
              the words "high"/"moderate"/"low" become 0.8/0.5/0.2. Measured on two real results (jobs
              10620ccd-07b and 5a362e56-077, fetched 2026-11-19): NEITHER carries `radar_summary`, so every radar
              :3000 draws for them is that fallback. A radar is read as a measurement; this one draws an axis only
              where BioQL stated the number, and leaves the rest as spokes with their reason.
  series      `iterations[]` with a numeric index and numeric metrics — ChatMessage's
              `renderOptimizationConvergence`, which reads `iterations[].iteration` and `.convergence`. That is the
              ONLY shape here that yields a series: `QuantumCalibrationCard`'s `points[]` has a STRING axis
              (`system`), so it is a table, as it is in BioQL.
  text        prose BioQL wrote, and every announcement this module makes about what it cut
  links       `downloads.files` and `open_in` (ArtifactsSection / ArtifactDownloads) — relative included, because
              BioQL's own values are relative `/api/...` routes the window resolves against the backend
  absent      a field BioQL did not compute, with BioQL's own reason: the honest-value contract of
              `HonestEngineCards.tsx` is `{value, units, method, provenance, is_estimate, disclaimer, null_reason}`
              and its rule is "ALWAYS show null_reason instead of hiding nulls"

The four rules that make it honest:
  1. `absent` is a first-class block. A field BioQL did not compute is DRAWN, with its reason — never dropped, never
     0, never "N/A", never a blank cell. Inside a table a missing cell carries the word "absent" and the column gets
     an `absent` block naming the rows and the reason, because a null cell IS the blank cell the rule forbids.
  2. Provenance is copied, never inferred. `is_real_hardware` comes only from a real boolean under that exact key —
     a backend string naming a device is NOT a hardware claim here (the verifier's job, not this module's). An
     absent flag stays null, a value BioQL wrote (`"NONE"`) is copied verbatim, and two answers that disagree stay
     "mixed" whichever side saw the disagreement.
  3. A verdict is never invented. `verdict`/`findings` are copied from `meta` when the caller passes them (the
     adapter already put the verifier's output there) and are `None`/`[]` otherwise, so the window says
     "not recorded" rather than "clean".
  4. A number keeps its unit, and only the unit the payload states: from the honest-value `units`/`unit`, from a
     sibling `<key>_units`, or from a key suffix that can only be a unit (`_kcal_mol`, `_nM`, `_A2`, `_ms`). BioQL's
     frontend also has a METRIC_GLOSSARY that supplies units "when the data omits them" (kd -> M, half_life -> h);
     that is a guess and is deliberately NOT ported. No unit beats a guessed one. A bare `_A` is not a unit either:
     `contacts_chain_A` is a chain, not an angstrom.

And rule 5: nothing returns an empty card. One recursive pass draws every leaf the detectors did not consume, with a
dotted label, so an unknown payload still shows its keys and their values — and rule 6: wherever a cap bites, a
`text` block says how many were left out. A cut that nobody is told about is the same defect as a missing field.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

KINDS = ("molecule", "admet", "docking", "structure", "screening", "pipeline", "job", "catalogue", "table", "text")
BLOCK_TYPES = ("facts", "table", "molecule2d", "molecule3d", "chart_image", "status", "gates", "radar",
               "series", "text", "links", "absent")

MAX_TABLE_ROWS = 200
MAX_SERIES_POINTS = 2000
MAX_FACTS = 120
MAX_SERIES_BLOCKS = 3
MAX_LINKS = 60
MAX_TEXT = 4000
MAX_SCALARS_IN_A_LIST = 40
MAX_DEPTH = 12

# The word that stands in a table cell the payload did not fill. A word, because `null`, `0`, `""` and `"N/A"` are
# the four things rule 1 forbids; the column's `absent` block carries the reason.
ABSENT_CELL = "absent"

# Units, matched on the KEY's suffix and CASE-SENSITIVELY: `_nM` is nanomolar, `_nm` is nanometres, and `length_aa`
# must not read as angstrom. Longest suffix first. Every entry is a suffix that can ONLY be a unit — no `_A`
# (chain A), no bare `_kcal` (the "/mol" would be ours, not the payload's), no `_M`, no `_s`.
UNIT_SUFFIX: tuple[tuple[str, str], ...] = (
    ("_kcal_per_mol", "kcal/mol"), ("_kcal_mol", "kcal/mol"), ("_kcalmol", "kcal/mol"),
    ("_kj_mol", "kJ/mol"), ("_mg_kg", "mg/kg"), ("_ml_min_kg", "mL/min/kg"),
    ("_percent", "%"), ("_pct", "%"),
    ("_nM", "nM"), ("_uM", "uM"), ("_pM", "pM"), ("_mM", "mM"), ("_nm", "nm"),
    ("_Ha", "Ha"), ("_eV", "eV"), ("_kDa", "kDa"),
    ("_A2", "Å²"), ("_A3", "Å³"),
    ("_ms", "ms"), ("_ns", "ns"), ("_ps", "ps"),
    ("_hours", "h"), ("_days", "d"), ("_minutes", "min"),
    ("_nt", "nt"), ("_aa", "aa"), ("_bp", "bp"),
)

# A key whose value is prose, never a measurement: never carries a unit.
PROSE_KEYS = ("note", "notes", "reason", "why", "description", "interpretation", "message", "disclaimer", "label",
              "comment", "caption", "method", "provenance", "detail", "details", "null_reason")

# The honest-value fields that explain a number rather than being one. They travel in the row's `note`.
HONEST_NOTE_KEYS = ("method", "provenance", "disclaimer", "note", "interpretation", "is_estimate", "ci90")

HTTP_URL = re.compile(r"^https?://", re.I)
# A path on this origin: one leading slash, never two. `//host/x` in an anchor is a protocol-relative URL to
# ANOTHER origin, and a backslash is read as a slash by browsers, so both are refused.
SAME_ORIGIN_PATH = re.compile(r"^/(?!/)[^\s\\]*$")
PDB_MARK = re.compile(r"^(HEADER|ATOM|HETATM|MODEL|CRYST1)\b|\nATOM\s", re.M)
# An index a series can run along. The x axis has to be a number the payload wrote, not a row counter we invented.
INDEX_KEYS = ("iteration", "step", "cycle", "epoch", "generation", "round", "x", "time", "t")

PROVENANCE_JOB_KEYS = ("ibm_job_id", "ibm_job_ids", "qpu_job_id", "hardware_job_id")
PROVENANCE_BACKEND_KEYS = ("quantum_backend", "backend_name", "ibm_backend", "backend")
IMAGE_KEYS = ("image_base64", "png_base64", "image")
SDF_KEYS = ("mol", "sdf", "mol_block", "mol_3d", "sdf_text")
PDB_KEYS = ("pdb", "pdb_data", "complex_pdb", "pdb_text")

# A 3D structure, and the FORMAT read off the key rather than sniffed out of the text. Measured on a real
# `quantum_antibody_discovery` result (job 10620ccd-07b, 2026-07-30): the structure BioQL returns is not one file
# but a pair — `mol_3d.ligand_sdf` (1.6 KB) and `mol_3d.protein_pdb` (129 KB) — which is what BioQL's own window
# gives its viewer, a ligand inside its protein. Carrying only SDF, as this module did, dropped the protein and
# named the ligand "SDF only"; the card had no structure at all for that payload.
# Longest and most specific key first, because `pdb` is a suffix of nothing but `protein_pdb` IS a key of its own.
STRUCT_KEYS: tuple[tuple[str, str, str], ...] = (
    ("ligand_sdf", "sdf", "ligand"), ("ligand_mol", "sdf", "ligand"),
    ("protein_pdb", "pdb", "protein"), ("receptor_pdb", "pdb", "protein"),
    ("complex_pdb", "pdb", "complex"), ("pocket_pdb", "pdb", "pocket"),
    ("mol_block", "sdf", "molecule"), ("sdf_text", "sdf", "molecule"),
    ("pdb_text", "pdb", "protein"), ("pdb_data", "pdb", "protein"),
    ("mmcif", "cif", "structure"), ("cif", "cif", "structure"),
    ("mol_3d", "sdf", "molecule"),        # a STRING under this key is the structure itself; a dict under it is
                                          # a container, and `_walk` finds its `ligand_sdf` / `protein_pdb`
    ("sdf", "sdf", "molecule"), ("mol", "sdf", "molecule"), ("pdb", "pdb", "protein"),
)
# What each format must look like if the key is telling the truth. A key that says `ligand_sdf` over PDB text is a
# contradiction, and the card refuses it with the reason rather than handing a viewer a file it will misread.
STRUCT_MARK = {
    "sdf": re.compile(r"^.*\n.*\n.*\n\s*\d+\s+\d+.*V[23]000|\$\$\$\$|M  END", re.S),
    "pdb": PDB_MARK,
    "cif": re.compile(r"^data_|_atom_site\.", re.M),
}
# One structure per card. A protein is ~129 KB and a result carries one per candidate; ten of them would be a card
# nobody can load. The rest are named and counted, never silently dropped.
MAX_STRUCT_BYTES = 4 * 1024 * 1024


# --------------------------------------------------------------------------- small readers
def _is_scalar(v: Any) -> bool:
    return v is None or isinstance(v, (str, int, float, bool))


def _bad_number(v: Any) -> bool:
    """NaN and infinity. `bionics_bridge` writes `float("nan")` where a prediction is missing, and a NaN that reaches
    a card is both unprintable and not JSON — so it is an ABSENT value, not a number."""
    return isinstance(v, float) and not math.isfinite(v)


def _numeric(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _prose(key: str) -> bool:
    return str(key).lower().rsplit(".", 1)[-1] in PROSE_KEYS


def _honest(v: Any) -> bool:
    """Does this object look like BioQL's honest-value contract? (`HonestEngineCards.isHonestValue`)"""
    if not isinstance(v, dict):
        return False
    if "value" in v or "null_reason" in v:
        return True
    return "method" in v and ("provenance" in v or "disclaimer" in v)


def _unit_for(key: str, value: Any, sibling: dict | None = None) -> str | None:
    """The unit the PAYLOAD states, or nothing. Never a guess from what the metric usually is."""
    if isinstance(value, dict):
        for k in ("units", "unit"):
            u = value.get(k)
            if isinstance(u, str) and u.strip():
                return u.strip()
    leaf = str(key).rsplit(".", 1)[-1]
    if isinstance(sibling, dict):
        for k in (f"{leaf}_units", f"{leaf}_unit"):
            u = sibling.get(k)
            if isinstance(u, str) and u.strip():
                return u.strip()
    # A suffix only names the unit of a MEASUREMENT. `note_nt` is prose that happens to end in a unit's letters, and
    # "sequence trimmed nt" is a card saying something the payload never said.
    inner = value.get("value") if isinstance(value, dict) else value
    if _prose(leaf) or not _numeric(inner):
        return None
    for suffix, unit in UNIT_SUFFIX:
        if leaf.endswith(suffix):
            return unit
    return None


def _note_for(value: Any) -> str | None:
    """What an honest value says ABOUT its number: method, provenance, the estimate flag, the 90% interval. BioQL
    renders these as a tooltip; here they travel in the row's note, because dropping them loses the one thing that
    says whether the number is measured or guessed."""
    if not isinstance(value, dict):
        return None
    bits: list[str] = []
    for k in HONEST_NOTE_KEYS:
        v = value.get(k)
        if v is None or v is False or v == "" or v == []:
            continue
        if k == "is_estimate":
            bits.append("estimate")
        elif k == "ci90" and isinstance(v, (list, tuple)) and len(v) >= 2:
            bits.append(f"ci90 {v[0]}–{v[1]}")
        elif isinstance(v, str):
            bits.append(v.strip())
    out = " · ".join(b for b in bits if b)
    return out[:300] or None


def _honest_absent(value: Any) -> str | None:
    """The reason this value is missing, in BioQL's own words, or None when it is a value.

    The order is the frontend's: `null_reason || reason || disclaimer` (PipelineResultCards.formatValue), then the
    section order `reason || null_reason || context` of HonestEngineCards. When there is no reason anywhere, the
    answer states the literal fact — what the payload holds — and never invents a cause.
    """
    if value is None:
        return "null in the payload, with no reason given"
    if _bad_number(value):
        return "not a number in the payload (NaN or infinity)"
    if isinstance(value, str) and value.strip().lower() in ("", "n/a", "na"):
        # Only the spellings BioQL's own frontend renders as "N/A". `"NONE"` is NOT one of them: it is BioQL's word
        # for "no quantum backend", an honest denial a reader is not misled by (studio/claims.py), and turning it
        # into an absence would throw away the one field that says what really ran.
        return f"the payload holds {value!r}, with no reason given"
    if isinstance(value, list) and not value:
        # `ibm_job_ids: []` is not a null: the payload answered, and what it answered is "none of these".
        return "the payload holds an empty list"
    if _honest(value) and (value.get("value") is None or _bad_number(value.get("value"))):
        for k in ("null_reason", "reason", "disclaimer", "context", "note"):
            r = value.get(k)
            if isinstance(r, str) and r.strip():
                return r.strip()
        return "null in the payload, with no reason given"
    if isinstance(value, dict) and not value:
        return "the payload holds an empty object"
    return None


def _walk(o: Any, path: str = "", depth: int = 0, limit: int = MAX_DEPTH):
    """Every (path, value) in the payload. Lists are NOT truncated here: a cut walk could read a payload that
    contradicts itself as consistent."""
    yield path, o
    if depth >= limit:
        return
    if isinstance(o, dict):
        for k, v in o.items():
            yield from _walk(v, f"{path}.{k}" if path else str(k), depth + 1, limit)
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from _walk(v, f"{path}[{i}]", depth + 1, limit)


def _leaf_key(path: str) -> str:
    return path.rsplit(".", 1)[-1].split("[")[0]


def _first(payload: Any, *names: str) -> tuple[str | None, Any]:
    """The shallowest value whose KEY is exactly one of these names. By token, never by substring: `"kd" in path`
    also matches `score_breakdown`, which once inflated an audit from 60% to 94% (CLAUDE.md)."""
    best: tuple[int, str | None, Any] = (10 ** 6, None, None)
    for path, value in _walk(payload):
        if _leaf_key(path) in names and value not in (None, "", [], {}):
            rank = path.count(".") + path.count("[")
            if rank < best[0]:
                best = (rank, path, value)
    return best[1], best[2]


def _parent_of(payload: Any, path: str) -> Any:
    """The container a path sits in, so a sibling (`format` next to `image_base64`) can be read as a sibling and not
    as "somewhere in the payload"."""
    if not path:
        return None
    cur = payload
    parts = re.findall(r"\[(\d+)\]|\.?([^.\[]+)", path)
    for idx, name in parts[:-1]:
        try:
            cur = cur[int(idx)] if idx else cur[name]
        except (KeyError, IndexError, TypeError):
            return None
    return cur


def _dicts_with(payload: Any, *names: str) -> list[tuple[str, dict]]:
    """Every dict that has one of these keys, shallowest first."""
    out = [(p, v) for p, v in _walk(payload) if isinstance(v, dict) and any(n in v for n in names)]
    return sorted(out, key=lambda pv: pv[0].count(".") + pv[0].count("["))


# --------------------------------------------------------------------------- provenance (rule 2)
def provenance(payload: Any) -> dict:
    """What BioQL ITSELF said about where the work ran. Copied, never inferred.

    `is_real_hardware` is read only from a real boolean under that exact key. A backend string that names a device
    (`ibm_fez`) is recorded as a backend and NOTHING more: turning a name into a hardware claim is precisely the
    inference this card must not make, and the verifier is the piece that judges a name against a job id. An absent
    flag stays null, and two answers that disagree are "mixed" rather than one of them.
    """
    flags: list[bool] = []
    jobs: list[str] = []
    backends: list[str] = []
    for path, value in _walk(payload):
        key = _leaf_key(path)
        if key == "is_real_hardware" and isinstance(value, bool):
            flags.append(value)
        elif key in PROVENANCE_JOB_KEYS and value:
            for j in (value if isinstance(value, list) else [value]):
                if isinstance(j, str) and j.strip() and j not in jobs:
                    jobs.append(j)
        elif key in PROVENANCE_BACKEND_KEYS and isinstance(value, str) and value.strip():
            if value not in backends:
                backends.append(value)        # verbatim, "NONE" included: it is BioQL's own word
    said = sorted(set(flags))
    return {"is_real_hardware": None if not said else (said[0] if len(said) == 1 else "mixed"),
            "ibm_job_ids": jobs, "backends": backends}


def _provenance_of(payload: Any, given: Any) -> dict:
    """This module's own walk of the payload, widened by what the caller already copied out of the same answer (the
    adapter records the backends under `quantum_backends`; the card's key is `backends` either way).

    Union, never override. "mixed" on EITHER side stays "mixed": the adapter's walk caps long lists, so it can see
    one claim where this walk sees two that disagree, and letting it override would turn a contradiction into a
    clean `true` — the degradation rule 2 exists to prevent.
    """
    mine = provenance(payload)
    if not isinstance(given, dict):
        return mine
    theirs = given.get("is_real_hardware")
    flags = [f for f in (mine["is_real_hardware"], theirs) if isinstance(f, bool)]
    if mine["is_real_hardware"] == "mixed" or theirs == "mixed" or len(set(flags)) > 1:
        real: Any = "mixed"
    else:
        real = flags[0] if flags else None
    jobs = list(mine["ibm_job_ids"])
    for j in (given.get("ibm_job_ids") or []):
        if isinstance(j, str) and j and j not in jobs:
            jobs.append(j)
    backends = list(mine["backends"])
    for b in (given.get("backends") or given.get("quantum_backends") or []):
        if isinstance(b, str) and b and b not in backends:
            backends.append(b)
    return {"is_real_hardware": real, "ibm_job_ids": jobs, "backends": backends}


# --------------------------------------------------------------------------- blocks
def _short_json(v: Any, limit: int = 200) -> str:
    try:
        s = json.dumps(v, ensure_ascii=False, default=str, allow_nan=False)
    except ValueError:
        s = json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + f"… (+{len(s) - limit} characters)"


def _text_block(s: str, label: str = "") -> dict:
    body = s if len(s) <= MAX_TEXT else s[:MAX_TEXT] + f"\n… {len(s) - MAX_TEXT} further characters are not shown."
    return {"type": "text", "text": f"{label}: {body}" if label else body}


def _cut(what: str, left_out: int, total: int, cap: int, unit: str) -> dict:
    return {"type": "text", "text": f"{left_out} of {total} {unit} of {what} are not shown "
                                    f"(the card holds at most {cap})."}


def _uniform_objects(v: Any) -> bool:
    """A list worth a table: dict entries that share at least one key, or one dict with several keys."""
    if not isinstance(v, list) or not v:
        return False
    items = [x for x in v if isinstance(x, dict)]
    if not items or len(items) * 2 < len(v):
        return False
    if len(items) == 1:
        return len(items[0]) > 1
    return bool(set(items[0]) & set(items[1]))


def _table(items: list, label: str) -> list[dict]:
    """A list of uniform objects as a table, with one `absent` block per column that has a missing cell.

    A null in a cell is the empty cell rule 1 forbids, so the cell carries the word "absent" and the column says
    which rows and why. Grouping by column is what keeps this bounded: one block per column, not one per cell.
    """
    rows_in = [r for r in items if isinstance(r, dict)]
    others = [r for r in items if not isinstance(r, dict)]
    columns: list[str] = []
    for r in rows_in:
        for k in r:
            if k not in columns and not (k.endswith("_units") or k.endswith("_unit")):
                columns.append(k)
    if not columns:
        return [_text_block(_short_json(items), label)]
    kept = rows_in[:MAX_TABLE_ROWS]
    body: list[list] = []
    missing: dict[str, list[tuple[int, str]]] = {}
    for i, r in enumerate(kept):
        line = []
        for c in columns:
            raw = r.get(c)
            why = _honest_absent(raw) if c in r else "the row does not carry this field"
            if why is not None:
                missing.setdefault(c, []).append((i, why))
                line.append(ABSENT_CELL)
                continue
            v = raw.get("value") if _honest(raw) else raw
            line.append(v if _is_scalar(v) and not isinstance(v, str) else
                        (v if isinstance(v, str) and len(v) <= 200 else _short_json(v)))
        body.append(line)
    # The unit belongs to the column, so it is said once, in the header.
    head = []
    for c in columns:
        sample = next((r[c] for r in kept if c in r and _honest_absent(r.get(c)) is None), None)
        unit = _unit_for(c, sample, rows_in[0] if rows_in else None)
        head.append(f"{c} ({unit})" if unit else c)
    out: list[dict] = [{"type": "table", "columns": head, "rows": body}]
    for c, hits in missing.items():
        whys = sorted({w for _, w in hits})
        where = "every row" if len(hits) == len(kept) else "rows " + ", ".join(str(i) for i, _ in hits[:20])
        out.append({"type": "absent", "what": f"{label}.{c}", "why": f"{where}: {'; '.join(whys)[:400]}"})
    if len(rows_in) > len(kept):
        out.append(_cut(label, len(rows_in) - len(kept), len(rows_in), MAX_TABLE_ROWS, "rows"))
    if others:
        out.append(_text_block(_short_json(others), f"{label} (entries that are not records)"))
    return out


def _series_blocks(items: list, label: str) -> list[dict]:
    """A list of records with a numeric index and numeric metrics, as one series per metric."""
    rows = [r for r in items if isinstance(r, dict)]
    if len(rows) < 2:
        return []
    index = next((k for k in INDEX_KEYS if all(_numeric(r.get(k)) for r in rows)), None)
    if index is None:
        return []
    metrics = [k for k in rows[0] if k != index and not _prose(k) and all(_numeric(r.get(k)) for r in rows)]
    out: list[dict] = []
    for metric in metrics[:MAX_SERIES_BLOCKS]:
        unit = _unit_for(metric, rows[0].get(metric), rows[0])
        out.append({"type": "series", "x_label": index,
                    "y_label": f"{label}.{metric} ({unit})" if unit else f"{label}.{metric}",
                    "points": [[r[index], r[metric]] for r in rows[:MAX_SERIES_POINTS]]})
        if len(rows) > MAX_SERIES_POINTS:
            out.append(_cut(f"{label}.{metric}", len(rows) - MAX_SERIES_POINTS, len(rows), MAX_SERIES_POINTS,
                            "points"))
    if len(metrics) > MAX_SERIES_BLOCKS:
        out.append({"type": "text",
                    "text": f"{len(metrics) - MAX_SERIES_BLOCKS} further numeric series in {label} are not drawn: "
                            f"{', '.join(metrics[MAX_SERIES_BLOCKS:][:12])}."})
    return out


def _links(payload: Any, consumed: set[str]) -> list[dict]:
    """An artifact BioQL declares in `downloads.files` or `open_in` is a link whether its value is relative or
    absolute — BioQL's own `ArtifactDownloads` keeps `/api/...` relative and the window resolves it against the
    backend. A `*_url` ANYWHERE ELSE is a route, and only an absolute one becomes a link: a relative route such as
    `poll_url: "/api/pipeline/job/x"` is drawn as a fact, because Studio follows the allowlist's route and never one
    a body names.
    """
    items: list[dict] = []
    seen: set[str] = set()
    for path, value in _walk(payload):
        key = _leaf_key(path)
        declared = key in ("files", "open_in") and isinstance(value, dict)
        pairs = list(value.items()) if declared else \
            [(key, value)] if key.endswith("_url") and isinstance(value, str) else []
        for label, url in pairs:
            if isinstance(url, list):
                url = url[0] if url and isinstance(url[0], str) else None
            if not isinstance(url, str) or not url.strip():
                continue
            # A link is drawn as an anchor by the window, so the scheme decides whether this is a download or a
            # script. `javascript:`, `data:` and `vbscript:` are rejected, and so is a protocol-relative `//host`,
            # which an anchor reads as another ORIGIN rather than a path on this one. A declared artifact may be a
            # same-origin path; anything else must be an absolute http(s) URL.
            url = url.strip()
            if not (HTTP_URL.match(url) or (declared and SAME_ORIGIN_PATH.match(url))) or url in seen:
                continue
            seen.add(url)
            items.append({"label": str(label), "url": url})
        if declared and pairs:
            consumed.add(path)
    out: list[dict] = []
    if items:
        out.append({"type": "links", "items": items[:MAX_LINKS]})
        if len(items) > MAX_LINKS:
            out.append(_cut("the download list", len(items) - MAX_LINKS, len(items), MAX_LINKS, "links"))
    return out


# --------------------------------------------------------------------------- the recursive pass (rules 5 and 6)
class _Draw:
    """One recursive walk that draws everything the detectors did not consume.

    Dotted labels all the way down, the same caps everywhere, and a `text` block for every cut — a payload twelve
    stages deep lands in the card, and whatever does not fit is counted out loud. The hand-unrolled two levels this
    replaces silently lost the third.
    """

    def __init__(self, consumed: set[str]) -> None:
        self.consumed = consumed
        self.rows: list[dict] = []
        self.gone: list[dict] = []
        self.blocks: list[dict] = []
        self.deeper = 0
        self.dropped_rows = 0

    def run(self, value: Any, label: str = "", path: str = "", depth: int = 0,
            parent: dict | None = None) -> None:
        if path and path in self.consumed:
            return
        why = _honest_absent(value)
        if why is not None:
            self.gone.append({"type": "absent", "what": label or "payload", "why": why})
            return
        if _honest(value):
            self._row(label, value.get("value"), value, parent)
            return
        if _is_scalar(value):
            self._row(label, value, None, parent)
            return
        if depth >= MAX_DEPTH:
            self.deeper += 1
            self.blocks.append(_text_block(_short_json(value), label))
            return
        if isinstance(value, dict):
            for k, v in value.items():
                if k.endswith("_units") or k.endswith("_unit"):
                    continue                     # carried as the unit of its own field; counted if orphaned
                self.run(v, f"{label}.{k}" if label else str(k), f"{path}.{k}" if path else str(k), depth + 1,
                         parent=value)
            for k in value:
                stem = k[:-6] if k.endswith("_units") else k[:-5] if k.endswith("_unit") else None
                if stem is not None and stem not in value:
                    # An orphan `x_units` with no `x` beside it is data, not a unit: counted, never dropped.
                    self._row(f"{label}.{k}" if label else k, value[k], None, None)
            return
        if isinstance(value, list):
            if _uniform_objects(value):
                rich = [x for x in value if isinstance(x, dict)
                        and any(isinstance(v, (dict, list)) and v for v in x.values())]
                if len(value) <= 3 and rich:
                    # A table flattens a nested row to JSON. `results[0].admet_classical.backend` is exactly the
                    # field that must not become a truncated string, so a short list of rich records is drawn in
                    # depth instead.
                    for i, v in enumerate(value):
                        self.run(v, f"{label}[{i}]" if label else f"[{i}]",
                                 f"{path}[{i}]" if path else f"[{i}]", depth + 1)
                    return
                self.blocks += _series_blocks(value, label or "rows")
                self.blocks += _table(value, label or "rows")
                return
            if all(_is_scalar(x) for x in value):
                kept = value[:MAX_SCALARS_IN_A_LIST]
                self._row(label, ", ".join("" if x is None else str(x) for x in kept), None, parent=None)
                if len(value) > len(kept):
                    self.blocks.append(_cut(label or "the list", len(value) - len(kept), len(value),
                                            MAX_SCALARS_IN_A_LIST, "entries"))
                return
            for i, v in enumerate(value[:MAX_TABLE_ROWS]):
                self.run(v, f"{label}[{i}]", f"{path}[{i}]", depth + 1)
            if len(value) > MAX_TABLE_ROWS:
                self.blocks.append(_cut(label or "the list", len(value) - MAX_TABLE_ROWS, len(value),
                                        MAX_TABLE_ROWS, "entries"))
            return
        self.blocks.append(_text_block(_short_json(value), label))

    def _row(self, label: str, value: Any, wrapper: Any, parent: dict | None) -> None:
        why = _honest_absent(value)
        if why is not None:
            self.gone.append({"type": "absent", "what": label or "payload", "why": why})
            return
        if isinstance(value, str) and len(value) > 200:
            self.blocks.append(_text_block(value, label))     # a structure file is not a fact
            return
        if len(self.rows) >= MAX_FACTS:
            self.dropped_rows += 1
            return
        row: dict = {"label": label or "value", "value": value}
        unit = _unit_for(label, wrapper if wrapper is not None else value, parent)
        if unit:
            row["unit"] = unit
        note = _note_for(wrapper)
        if note:
            row["note"] = note
        self.rows.append(row)

    def out(self) -> list[dict]:
        out: list[dict] = []
        if self.rows:
            out.append({"type": "facts", "rows": self.rows})
        out += self.blocks
        out += self.gone[:MAX_FACTS]
        if self.dropped_rows:
            out.append(_cut("this payload", self.dropped_rows, self.dropped_rows + len(self.rows), MAX_FACTS,
                            "facts"))
        if len(self.gone) > MAX_FACTS:
            out.append(_cut("the absent fields", len(self.gone) - MAX_FACTS, len(self.gone), MAX_FACTS, "entries"))
        if self.deeper:
            out.append({"type": "text", "text": f"{self.deeper} branch(es) deeper than {MAX_DEPTH} levels are shown "
                                                f"as JSON rather than drawn field by field."})
        return out


# --------------------------------------------------------------------------- shape detectors
def _dict_at(payload: Any, path: str | None, value: Any) -> dict | None:
    return value if isinstance(value, dict) else None


def _molecule_blocks(payload: Any, consumed: set[str]) -> list[dict]:
    """A 2D depiction and a 3D structure, each only where the payload states the format it really is."""
    out: list[dict] = []
    b64_path, b64 = _first(payload, *IMAGE_KEYS)
    smiles_path, smiles = _first(payload, "smiles", "canonical_smiles")
    if isinstance(b64, str) and b64.strip():
        parent = _parent_of(payload, b64_path or "")
        fmt = parent.get("format") or parent.get("image_format") if isinstance(parent, dict) else None
        name = str(fmt).strip().lower() if isinstance(fmt, str) else ""
        if "chart" in (b64_path or "").lower():
            # `visualization.charts[].image_base64` is a plot, not a depiction of the molecule. It used to be
            # refused for want of a block type — but BioQL has ALREADY drawn this chart, and throwing away the
            # engine's own picture to redraw a worse one from the numbers would be the card preferring itself to
            # the engine. It gets its own block, and `molecule2d` stays what it says it is.
            consumed.add(b64_path)
        elif name in ("png", "image/png") or b64.startswith("data:image/png"):
            block: dict = {"type": "molecule2d", "png_base64": b64}
            if isinstance(smiles, str) and smiles.strip():
                block["smiles"] = smiles.strip()
            out.append(block)
            consumed.add(b64_path)
        else:
            out.append({"type": "absent", "what": "molecule2d",
                        "why": f"the payload carries an image the card cannot claim is a PNG: the sibling of "
                               f"{b64_path} states format {fmt!r}"})
            out.append({"type": "facts", "rows": [{"label": f"{b64_path} (base64 characters)", "value": len(b64)}]})
            consumed.add(b64_path)
    if not any(b.get("type") == "molecule2d" for b in out) and isinstance(smiles, str) and smiles.strip():
        out.append({"type": "molecule2d", "smiles": smiles.strip()})

    out += _structure_blocks(payload, consumed)
    if smiles_path and any(b.get("type") in ("molecule2d", "molecule3d") for b in out):
        consumed.add(smiles_path)
    return out


def _struct_parts(payload: Any) -> list[tuple[str, str, str, str]]:
    """Every 3D structure in the payload as (path, role, format, text), shallowest first.

    The format comes from the key — `ligand_sdf` is SDF because BioQL named it so — and the text is then CHECKED
    against that format. A key and its contents that disagree are not guessed between: the caller refuses the pair
    and says which key and which format, because a mislabelled structure handed to a viewer is drawn wrong rather
    than not drawn.
    """
    found: list[tuple[int, str, str, str, str]] = []
    seen: set[str] = set()
    # BioQL sometimes hands the viewer its own embed: `visualization.molecular.embed_3dmol` is
    # `{data: "<pdb text>", format: "pdb", style: "cartoon"}` — measured on job 10620ccd-07b. There the format is
    # a SIBLING and not the key, which is the one place this module reads it from anywhere but the key, because
    # BioQL stated it right there.
    for path, value in _walk(payload):
        if not isinstance(value, dict):
            continue
        data, fmt = value.get("data"), value.get("format")
        if isinstance(data, str) and data.strip() and isinstance(fmt, str) \
                and fmt.strip().lower() in STRUCT_MARK:
            role = "protein" if fmt.strip().lower() in ("pdb", "cif") else "molecule"
            found.append((path.count(".") + path.count("["), f"{path}.data", role, fmt.strip().lower(), data))
            seen.add(f"{path}.data")
    for path, value in _walk(payload):
        if not isinstance(value, str) or not value.strip() or path in seen:
            continue
        key = _leaf_key(path)
        for name, fmt, role in STRUCT_KEYS:
            if key != name:
                continue
            seen.add(path)
            found.append((path.count(".") + path.count("["), path, role, fmt, value))
            break
    found.sort(key=lambda r: r[0])
    return [(p, role, fmt, text) for _, p, role, fmt, text in found]



def _chart_blocks(payload: Any, consumed: set[str]) -> list[dict]:
    """The charts BioQL drew itself. `visualization.charts[]` is `{title, image_base64, png_url}` — measured on a
    real result (job 10620ccd-07b: "Candidate Ranking" and one more) — and this carries them as the engine made
    them. The title is BioQL's; nothing here renames a plot."""
    out: list[dict] = []
    for path, value in _walk(payload):
        if not isinstance(value, dict):
            continue
        b64 = value.get("image_base64") or value.get("png_base64")
        if not isinstance(b64, str) or not b64.strip():
            continue
        if "chart" not in path.lower() and "chart" not in str(value.get("title", "")).lower():
            continue
        block = {"type": "chart_image", "png_base64": b64}
        if isinstance(value.get("title"), str):
            block["title"] = value["title"]
        url = value.get("png_url") or value.get("url")
        if isinstance(url, str) and SAME_ORIGIN_PATH.match(url):
            block["url"] = url
        out.append(block)
        consumed.add(path)
        for k in ("image_base64", "png_base64", "png_url", "url", "title"):
            consumed.add(f"{path}.{k}")
        if len(out) >= 8:
            break
    return out


# A word that states how something came out. Only these: a status this module cannot read is left as a fact, not
# coloured in by guessing which way it leans.
STATUS_GOOD = ("pass", "passed", "ok", "success", "go", "clean", "complete", "completed")
STATUS_BAD = ("fail", "failed", "error", "no-go", "no_go", "blocked", "contradicted", "rejected")


def _status_blocks(payload: Any, consumed: set[str]) -> list[dict]:
    """A verdict BioQL wrote, with its counters. `consistency_audit` answers
    `{status: "pass", n_issues: 0, n_critical: 0, issues: []}` and `visualization.card` answers `{verdict: "GO"}`;
    both are a sentence about the whole run, and both read as a line of facts among two hundred others."""
    out: list[dict] = []
    for path, value in _walk(payload):
        if not isinstance(value, dict):
            continue
        word = value.get("status") if isinstance(value.get("status"), str) else value.get("verdict")
        if not isinstance(word, str) or not word.strip():
            continue
        low = word.strip().lower()
        tone = "good" if low in STATUS_GOOD else "bad" if low in STATUS_BAD else "plain"
        counts = {k: v for k, v in value.items()
                  if k.startswith("n_") and isinstance(v, int) and not isinstance(v, bool)}
        issues = value.get("issues")
        block: dict = {"type": "status", "word": word.strip(), "tone": tone, "where": path or "result"}
        if counts:
            block["counts"] = counts
        if isinstance(issues, list):
            block["issues"] = [str(i)[:200] for i in issues[:12]]
            block["issues_total"] = len(issues)
        out.append(block)
        consumed.add(f"{path}.status" if "status" in value else f"{path}.verdict")
        for k in counts:
            consumed.add(f"{path}.{k}")
        if len(out) >= 4:
            break
    return out


def _gates_blocks(payload: Any, consumed: set[str]) -> list[dict]:
    """A gate and the reason BioQL gave for it. `qdiffvision.capability_gates` pairs every flag with its own
    `reason_<flag>` — `learned_se3_diffusion: true` next to "validated graph-specific checkpoint; fixed topology
    only". Drawn as a bare true/false it loses exactly the half that says what the claim is worth."""
    out: list[dict] = []
    for path, value in _walk(payload):
        if not isinstance(value, dict):
            continue
        flags = {k: v for k, v in value.items() if isinstance(v, bool)}
        reasons = {k[len("reason_"):]: v for k, v in value.items()
                   if k.startswith("reason_") and isinstance(v, str)}
        paired = [k for k in flags if k in reasons]
        if len(flags) < 2 or not paired:
            continue
        rows = []
        for k, v in flags.items():
            row = {"label": k, "open": bool(v)}
            why = reasons.get(k)
            if isinstance(why, str) and why.strip():
                row["why"] = why.strip()[:300]
            rows.append(row)
            consumed.add(f"{path}.{k}")
            consumed.add(f"{path}.reason_{k}")
        out.append({"type": "gates", "rows": rows, "where": path})
        if len(out) >= 3:
            break
    return out

def _structure_blocks(payload: Any, consumed: set[str]) -> list[dict]:
    """The 3D structure, whole. One block, carrying every part of the SAME structure — a ligand and the protein it
    sits in are one picture, which is what BioQL's own viewer is given.

    Never cut: a structure with lines missing is a different molecule, so a part too large to carry is named and
    left out with its size, and the rest still travels.
    """
    parts = _struct_parts(payload)
    if not parts:
        return []
    out: list[dict] = []
    first = parts[0][0]
    stem = first.rsplit(".", 1)[0] if "." in first else ""
    carried: list[dict] = []
    total = 0
    for path, role, fmt, text in parts:
        same = (path.rsplit(".", 1)[0] if "." in path else "") == stem
        if not same:
            continue                       # another candidate's structure: counted below, not merged into this one
        consumed.add(path)
        mark = STRUCT_MARK.get(fmt)
        if mark is not None and not mark.search(text):
            out.append({"type": "absent", "what": f"molecule3d ({role})",
                        "why": f"{path} is named {fmt} and its text does not read as {fmt}; the card will not hand "
                               f"a viewer a structure under a format the payload contradicts"})
            continue
        if total + len(text) > MAX_STRUCT_BYTES:
            out.append({"type": "absent", "what": f"molecule3d ({role})",
                        "why": f"{path} is {len(text)} bytes and the card carries at most {MAX_STRUCT_BYTES}; "
                               f"a structure is never cut, so this part is left out whole"})
            continue
        total += len(text)
        carried.append({"role": role, "format": fmt, "data": text, "path": path, "bytes": len(text)})
    if carried:
        block: dict = {"type": "molecule3d", "parts": carried, "label": stem or first}
        only = [c for c in carried if c["format"] == "sdf"]
        if len(carried) == 1 and only:
            block["sdf"] = only[0]["data"]          # the one-SDF shape this block had before it carried parts
        out.insert(0, block)
    more = {p.rsplit(".", 1)[0] for p, _, _, _ in parts} - {stem}
    if more:
        out.append({"type": "text",
                    "text": f"{len(more)} further structure(s) in this result are not drawn, one card carries one: "
                            + ", ".join(sorted(more)[:6])})
    return out


# The eight axes of BioQL's own ADMET radar, in its own order, read off `ADMETRadarChart.tsx`'s `radar_summary`
# branch. The key is BioQL's; the label is the one its chart shows.
RADAR_AXES: tuple[tuple[str, str], ...] = (
    ("absorption", "Absorption"), ("distribution", "Distribution"), ("metabolism", "Metabolism"),
    ("excretion", "Excretion"), ("safety", "Safety"), ("drug_likeness", "Drug-likeness"),
    ("solubility", "Solubility"), ("selectivity", "Selectivity"),
)


def _radar_blocks(payload: Any, consumed: set[str]) -> list[dict]:
    """BioQL's ADMET radar, from the numbers BioQL computed and from nothing else.

    `ADMETRadarChart.tsx` clamps each score into 0..1 and that clamp is copied. What is NOT copied is its fallback:
    an axis BioQL did not state is drawn as a spoke with no vertex and named in an `absent` block, never as the
    0.5 that component uses, because a radar is read as an assessment and a shape made of defaults is the
    plausible-value-instead-of-stopping defect in a picture.
    """
    path, summary = _first(payload, "radar_summary")
    if not isinstance(summary, dict):
        return []
    consumed.add(path)
    axes: list[dict] = []
    missing: list[str] = []
    for key, label in RADAR_AXES:
        v = summary.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
            axes.append({"label": label, "value": None,
                         "why": f"{path}.{key} is not a number BioQL stated"})
            missing.append(label)
            continue
        axes.append({"label": label, "value": min(1.0, max(0.0, float(v)))})
    out: list[dict] = [{"type": "radar", "axes": axes, "source": path, "max": 1}]
    if missing:
        out.append({"type": "absent", "what": "radar axes",
                    "why": f"{', '.join(missing)} — {path} does not state them, and an axis drawn at a value this "
                           f"window chose would be a measurement nobody made"})
    return out


def _admet_blocks(payload: Any, consumed: set[str]) -> list[dict]:
    """BioQL's ADMET answer: `raw_predictions` is a flat object of TDC endpoints, and a missing endpoint is a null
    (or a `float("nan")`, which `bionics_bridge` writes) that must be DRAWN as absent with its reason."""
    out: list[dict] = []
    holders = _dicts_with(payload, "raw_predictions")
    for path, holder in holders:
        preds = holder.get("raw_predictions")
        if not isinstance(preds, dict) or not preds:
            continue
        label = f"{path}.raw_predictions" if path else "raw_predictions"
        out.append(_text_block(f"{len(preds)} endpoint(s) from {label}"))
        draw = _Draw(set())
        draw.run(preds)
        out += draw.out()
        consumed.add(f"{path}.raw_predictions" if path else "raw_predictions")
    return out


def _catalogue_blocks(payload: Any, consumed: set[str]) -> list[dict]:
    """The motif catalogue: `{categories: {name: [...]}, total_motifs: n}`, and the flat `{motifs: {name: smarts}}`
    the tools panel reads. The entries themselves are drawn, not just counted: a catalogue whose contents are gone
    is a number, not a catalogue."""
    out: list[dict] = []
    cat_path, cats = _first(payload, "categories")
    if isinstance(cats, dict) and cats:
        counts = [[name, len(items) if isinstance(items, (list, dict)) else 1] for name, items in cats.items()]
        out.append({"type": "table", "columns": ["category", "entries"], "rows": counts[:MAX_TABLE_ROWS]})
        if len(counts) > MAX_TABLE_ROWS:
            out.append(_cut("the categories", len(counts) - MAX_TABLE_ROWS, len(counts), MAX_TABLE_ROWS, "rows"))
        flat: list[list] = []
        total = 0
        for name, items in cats.items():
            entries = items if isinstance(items, list) else [items]
            total += len(entries)
            for e in entries:
                if len(flat) >= MAX_TABLE_ROWS:
                    continue
                if isinstance(e, dict):
                    flat.append([name, str(e.get("name") or e.get("id") or _short_json(e, 80)),
                                 str(e.get("smarts") or e.get("smiles") or e.get("pattern") or "")])
                else:
                    flat.append([name, str(e), ""])
        if flat:
            out.append({"type": "table", "columns": ["category", "entry", "pattern"], "rows": flat})
            if total > len(flat):
                out.append(_cut("the catalogue entries", total - len(flat), total, MAX_TABLE_ROWS, "rows"))
        consumed.add(cat_path or "categories")
    mot_path, motifs = _first(payload, "motifs")
    if isinstance(motifs, dict) and motifs:
        rows = [[name, value if isinstance(value, str) else _short_json(value, 120)]
                for name, value in motifs.items()]
        out.append({"type": "table", "columns": ["motif", "pattern"], "rows": rows[:MAX_TABLE_ROWS]})
        if len(rows) > MAX_TABLE_ROWS:
            out.append(_cut("the motifs", len(rows) - MAX_TABLE_ROWS, len(rows), MAX_TABLE_ROWS, "rows"))
        consumed.add(mot_path or "motifs")
    return out


# --------------------------------------------------------------------------- kind and title
def _kind_of(payload: Any) -> str:
    """The card's kind, from the shape alone. Specific before general, and a hit has to be the right TYPE: a
    `scoring: {"admet": 0.3}` weight is a number, not an ADMET result, and reading it as one is how a 12-stage
    pipeline lost its structure to an ADMET card."""
    if not isinstance(payload, dict):
        return "table" if _uniform_objects(payload) else "text"
    def container(*names: str) -> bool:
        _, v = _first(payload, *names)
        return isinstance(v, (dict, list)) and bool(v)
    def present(*names: str) -> bool:
        return _first(payload, *names)[0] is not None
    if container("stages"):
        # Only a real stage structure. BioQL's ADMET envelope carries `pipeline: "ADMET_STANDALONE"` as a NAME, and
        # reading a name as a shape is the very dispatch-by-name this module refuses; the name titles the card.
        return "pipeline"
    if container("raw_predictions", "admet_classical", "admet_quantum", "admet"):
        return "admet"
    if container("categories", "motifs") or present("total_motifs"):
        return "catalogue"
    if container("docking", "poses") or present("binding_affinity_kcal_mol", "binding_affinity_kcal",
                                                "poses_generated"):
        return "docking"
    if container("structure_3d", "complex_structure_3d") or present("pdb_data", "complex_pdb", "plddt", "iptm"):
        return "structure"
    if present(*SDF_KEYS, *IMAGE_KEYS):
        return "molecule"
    if container("candidates", "screening", "hits", "ranked"):
        return "screening"
    if present("job_id", "poll_url") and not present("results", "result"):
        return "job"
    if payload:
        # Structured data with no shape Studio recognises. The blocks say what it is; "table" only states that there
        # is data rather than prose.
        return "table"
    return "text"


def _title_of(payload: Any, kind: str, tool: str | None) -> tuple[str, str | None]:
    """A name for the card, and a subtitle. The tool's name may reach the subtitle and NOTHING else: a tool that
    lies about what it is cannot change a kind or a block. The name is read from a TOP-LEVEL field only, so a card
    is never titled after the first entry of a nested catalogue."""
    label = {"admet": "ADMET", "catalogue": "Catalogue", "docking": "Docking", "structure": "Structure",
             "molecule": "Molecule", "pipeline": "Pipeline", "screening": "Screening", "job": "Job",
             "table": "Result", "text": "Result"}[kind]
    name = None
    if isinstance(payload, dict):
        for key in ("pipeline", "pipeline_name", "target", "job_id", "smiles", "name", "title"):
            v = payload.get(key)
            if isinstance(v, str) and v.strip():
                name = v.strip()[:80]
                break
    title = f"{label} · {name}" if name else label
    bits = [b for b in (tool.strip() if isinstance(tool, str) and tool.strip() else None,
                        f"{len(payload)} top-level field(s)" if isinstance(payload, dict) else
                        f"{len(payload)} entries" if isinstance(payload, list) else None) if b]
    return title, " · ".join(bits) or None


# --------------------------------------------------------------------------- the one public call
def card(payload: Any, tool: str | None = None, meta: dict | None = None) -> dict:
    """One BioQL result as a card spec. Never raises on a payload, and never returns a card with no blocks."""
    meta = meta if isinstance(meta, dict) else {}
    kind = _kind_of(payload)
    title, subtitle = _title_of(payload, kind, tool)

    consumed: set[str] = set()
    blocks: list[dict] = []
    if isinstance(payload, (dict, list)):
        # Every detector runs, whatever the kind says. They are guarded by the shapes they read, and gating them on
        # one kind is what let a pipeline carrying `mol_3d` be drawn as an ADMET card with no structure in it.
        blocks += _status_blocks(payload, consumed)
        blocks += _chart_blocks(payload, consumed)
        blocks += _gates_blocks(payload, consumed)
        blocks += _radar_blocks(payload, consumed)
        blocks += _admet_blocks(payload, consumed)
        blocks += _catalogue_blocks(payload, consumed)
        blocks += _molecule_blocks(payload, consumed)
        blocks += _links(payload, consumed)

    draw = _Draw(consumed)
    draw.run(payload)
    blocks += draw.out()
    if not blocks:
        # A payload with nothing in it is still drawn, and says what it was rather than nothing at all.
        blocks = [_text_block(f"the payload is {type(payload).__name__} and carries no fields"),
                  {"type": "absent", "what": "content", "why": "BioQL's answer held no fields to draw"}]

    verdict = meta.get("verdict")
    findings = meta.get("findings")
    return {"kind": kind, "title": title, "subtitle": subtitle,
            "provenance": _provenance_of(payload, meta.get("provenance")),
            "verdict": verdict if isinstance(verdict, str) and verdict.strip() else None,
            "findings": list(findings) if isinstance(findings, list) else [],
            "blocks": blocks}


# --------------------------------------------------------------------------- the sections of a result
# One BioQL result is not one card in BioQL's own window: `ChatMessage.tsx` splits it and draws a card per section —
# the ADMET one, the docking one, toxicology, pharmacology, the clinical simulation, the candidates, the audit.
# The dot-paths below are COPIED from `src/frontend/src/services/cardRegistry.ts`, which is BioQL's own manifest of
# which card fires on which shape, read on 2026-11-19 (43 entries). They are not guessed and they are not matched by
# tool name: a pipeline nobody has heard of gets its sections for free, exactly as BioQL intends.
#
# Measured against a real `quantum_antibody_discovery` result (job 10620ccd-07b): BioQL's window draws 12 cards for
# it and this module drew ONE, 276 blocks deep, with every section's numbers in the same undivided list.
SECTIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("candidates", "Candidates",
     ("candidates", "discovery.candidates", "candidates_generated.small_molecules",
      "candidates_generated.peptides", "candidates_generated.nanobodies")),
    ("admet", "ADMET",
     ("admet", "stages.2_admet.data", "analyses.admet", "best_candidate.analyses.admet", "best_candidate.admet",
      "candidates.0.analyses.admet", "candidates.0.admet")),
    ("docking", "Docking",
     ("docking", "stages.9_docking", "stages.6_docking", "analyses.docking",
      "best_candidate.analyses.docking", "best_candidate.docking",
      "candidates.0.analyses.docking", "candidates.0.docking")),
    ("toxicology", "Toxicology",
     ("toxicology", "stages.3_toxicology.data", "toxicology_study", "admet.toxicology", "analyses.toxicology",
      "best_candidate.analyses.toxicology", "best_candidate.toxicology", "best_candidate.admet.toxicology",
      "candidates.0.analyses.toxicology", "candidates.0.toxicology", "candidates.0.admet.toxicology",
      "discovery.toxicology", "stages.7_toxicology.data")),
    ("pharmacology", "Pharmacology",
     ("pharmacology", "stages.4_pharmacology.data", "pharmacology_study", "analyses.pharmacology",
      "best_candidate.analyses.pharmacology", "best_candidate.pharmacology",
      "candidates.0.analyses.pharmacology", "candidates.0.pharmacology", "discovery.pharmacology",
      "pharmacokinetics")),
    ("clinical", "Clinical simulation",
     ("clinical_trial", "stages.5_clinical_trial.data", "stages.clinical_trial_simulation.trials.0",
      "clinical_trials", "analyses.clinical_trial", "best_candidate.analyses.clinical_trial",
      "best_candidate.clinical_trial", "candidates.0.analyses.clinical_trial", "candidates.0.clinical_trial")),
    ("generative", "Generative model", ("trained_generator", "stages.iglm_proposer")),
    ("qdiffvision", "QDiffVision", ("qdiffvision",)),
    ("quantum_cell", "Quantum cell", ("quantum_cell", "cell_simulation")),
    ("consistency_audit", "Consistency audit", ("consistency_audit",)),
    ("visualization", "Visualization", ("visualization",)),
    ("structure", "Structure", ("mol_3d", "structure_3d", "best_candidate.mol_3d", "candidates.0.mol_3d",
                                "stages.10_3d_structure")),
)


def _at(payload: Any, path: str) -> Any:
    """A dot-path, with a numeric part reading a list index — `candidates.0.admet` is how the registry writes it."""
    cur = payload
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def _nonempty(v: Any) -> bool:
    """BioQL's own `isNonEmpty` (cardRegistry.ts): null, "", [] and {} are absent, and 0 and false are NOT."""
    if v is None or v == "":
        return False
    if isinstance(v, (list, dict, tuple)) and len(v) == 0:
        return False
    return True


def sections(payload: Any) -> list[tuple[str, str, str, Any]]:
    """(id, title, the path it was found at, the subtree) for every section BioQL's own manifest would fire on.

    The FIRST path that resolves wins, in the registry's own order, so a section is read where BioQL reads it and
    `best_candidate.admet` does not become a second ADMET card beside `admet`.
    """
    out: list[tuple[str, str, str, Any]] = []
    if not isinstance(payload, (dict, list)):
        return out
    for sid, title, paths in SECTIONS:
        for path in paths:
            v = _at(payload, path)
            if _nonempty(v):
                out.append((sid, title, path, v))
                break
    return out


def cards_of(payload: Any, tool: str | None = None, meta: dict | None = None) -> dict:
    """One result as the cards BioQL's own window draws for it: one per section, plus one for what is left.

    A section that is not there is simply not a card — never an empty one. What no section claimed still gets its
    card, because a number that falls between the sections must not disappear: that is how a result quietly loses
    the one field somebody was looking for.
    """
    found = sections(payload)
    if len(found) < 2:
        return card(payload, tool=tool, meta=meta)
    meta = meta if isinstance(meta, dict) else {}
    claimed = {path for _, _, path, _ in found}
    rest = payload
    if isinstance(payload, dict):
        rest = {k: v for k, v in payload.items() if k not in {p.split(".")[0] for p in claimed}}
    kids = []
    for sid, title, path, sub in found:
        c = card(sub, tool=f"{title} — {path}", meta=meta)
        c["title"] = title
        c["subtitle"] = path
        c["section"] = sid
        kids.append(c)
    if isinstance(rest, dict) and rest:
        c = card(rest, tool=tool, meta=meta)
        c["title"] = "The rest of the result"
        c["subtitle"] = "what no section claimed — nothing is dropped"
        c["section"] = "rest"
        kids.append(c)
    title, subtitle = _title_of(payload, _kind_of(payload), tool)
    return {"kind": "group", "title": title, "subtitle": f"{len(kids)} card(s) · {subtitle}" if subtitle else
            f"{len(kids)} card(s)", "cards": kids, "blocks": [],
            "provenance": kids[0].get("provenance") if kids else {},
            "verdict": meta.get("verdict"), "findings": meta.get("findings") or []}
