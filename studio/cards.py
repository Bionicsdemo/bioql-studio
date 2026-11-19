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
  table       a list of uniform objects — `results[]`, `candidates[]`, `guides[]`, `categories`
  molecule2d  `{image_base64, format: "png"}` (the `/api/render/2d` answer) and `visualization.molecular.image_base64`
  molecule3d  SDF text: `{mol: "<sdf>"}` (the `/api/smiles-to-3d` answer), `mol_3d`, `mol_block`
  series      `iterations[]` with a numeric index and numeric metrics (ChatMessage renderOptimizationConvergence),
              and `points[]` (QuantumCalibrationCard)
  text        prose BioQL wrote, and every announcement this module makes about what it cut
  links       `downloads.files` and `open_in` (ArtifactsSection) — absolute http(s) URLs only
  absent      a field BioQL did not compute, with BioQL's own reason: the honest-value contract of
              `HonestEngineCards.tsx` is `{value, units, method, provenance, is_estimate, disclaimer, null_reason}`
              and its rule is "ALWAYS show null_reason instead of hiding nulls"

The four rules that make it honest:
  1. `absent` is a first-class block. A field BioQL did not compute is DRAWN, with its reason — never dropped, never
     0, never "N/A", never a blank cell. Inside a table a missing cell carries the word "absent" and the column gets
     an `absent` block naming the rows and the reason, because a null cell IS the blank cell the rule forbids.
  2. Provenance is copied, never inferred. `is_real_hardware` comes only from a real boolean under that exact key —
     a backend string naming a device is NOT a hardware claim here (the verifier's job, not this module's). An
     absent flag stays null, and a value BioQL wrote (`"NONE"`) is copied verbatim.
  3. A verdict is never invented. `verdict`/`findings` are copied from `meta` when the caller passes them (the
     adapter already put the verifier's output there) and are `None`/`[]` otherwise, so the window says
     "not recorded" rather than "clean".
  4. A number keeps its unit, and only the unit the payload states: from the honest-value `units`/`unit`, from a
     sibling `<key>_units`, or from the key's own suffix (`_kcal_mol`, `_nM`, `_A2`, `_ms`). BioQL's frontend also
     has a METRIC_GLOSSARY that supplies units "when the data omits them" (kd -> M, half_life -> h); that is a guess
     and is deliberately NOT ported. No unit beats a guessed one.

And rule 5: nothing returns an empty card. Whatever the detectors recognise, a remainder pass walks every top-level
key they did not consume and draws it, so an unknown payload still shows its keys and their values.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

KINDS = ("molecule", "admet", "docking", "structure", "screening", "pipeline", "job", "catalogue", "table", "text")
BLOCK_TYPES = ("facts", "table", "molecule2d", "molecule3d", "series", "text", "links", "absent")

MAX_TABLE_ROWS = 200
MAX_SERIES_POINTS = 2000
MAX_FACTS = 120
MAX_SERIES_BLOCKS = 3
MAX_TEXT = 4000

# The word that stands in a table cell the payload did not fill. A word, because `null`, `0`, `""` and `"N/A"` are
# the four things rule 1 forbids; the column's `absent` block carries the reason.
ABSENT_CELL = "absent"

# Units, matched on the KEY's suffix and CASE-SENSITIVELY: `_nM` is nanomolar, `_nm` is nanometres, and `length_aa`
# must not read as ångström. Longest suffix first.
UNIT_SUFFIX: tuple[tuple[str, str], ...] = (
    ("_kcal_per_mol", "kcal/mol"), ("_kcal_mol", "kcal/mol"), ("_kcalmol", "kcal/mol"), ("_kcal", "kcal/mol"),
    ("_kj_mol", "kJ/mol"), ("_mg_kg", "mg/kg"), ("_ml_min_kg", "mL/min/kg"),
    ("_percent", "%"), ("_pct", "%"),
    ("_nM", "nM"), ("_uM", "uM"), ("_pM", "pM"), ("_mM", "mM"), ("_nm", "nm"),
    ("_Ha", "Ha"), ("_eV", "eV"), ("_kDa", "kDa"), ("_Da", "Da"),
    ("_A2", "Å²"), ("_A3", "Å³"), ("_A", "Å"),
    ("_ms", "ms"), ("_ns", "ns"), ("_us", "us"), ("_ps", "ps"),
    ("_hours", "h"), ("_days", "d"), ("_minutes", "min"), ("_seconds", "s"),
    ("_nt", "nt"), ("_aa", "aa"), ("_bp", "bp"),
)

# A key whose value is prose, never a measurement: never carries a unit and never reads as a missing number.
PROSE_KEYS = ("note", "notes", "reason", "why", "description", "interpretation", "message", "disclaimer", "label",
              "comment", "summary_text", "caption", "method", "provenance", "detail", "details")

SDF_MARK = re.compile(r"V[23]000|\$\$\$\$|^\s*\S*\n\s*\S*\n", re.M)
HTTP_URL = re.compile(r"^https?://", re.I)
# An index a series can run along. The x axis has to be a number the payload wrote, not a row counter we invented.
INDEX_KEYS = ("iteration", "step", "cycle", "epoch", "generation", "round", "x", "time", "t")

JOB_STATUS_KEYS = ("status", "state")
PROVENANCE_JOB_KEYS = ("ibm_job_id", "ibm_job_ids", "qpu_job_id", "hardware_job_id")
PROVENANCE_BACKEND_KEYS = ("quantum_backend", "backend_name", "ibm_backend", "backend")


# --------------------------------------------------------------------------- small readers
def _is_scalar(v: Any) -> bool:
    return v is None or isinstance(v, (str, int, float, bool))


def _bad_number(v: Any) -> bool:
    """NaN and infinity. `bionics_bridge` writes `float("nan")` where a prediction is missing, and a NaN that reaches
    a card is both unprintable and not JSON — so it is an ABSENT value, not a number."""
    return isinstance(v, float) and not math.isfinite(v)


def _prose(key: str) -> bool:
    return str(key).lower().rsplit(".", 1)[-1] in PROSE_KEYS


def _unit_for(key: str, value: Any, sibling: dict | None = None) -> str | None:
    """The unit the PAYLOAD states, or nothing. Never a guess from what the metric usually is."""
    if isinstance(value, dict):
        for k in ("units", "unit"):
            u = value.get(k)
            if isinstance(u, str) and u.strip():
                return u.strip()
    if isinstance(sibling, dict):
        for k in (f"{key}_units", f"{key}_unit"):
            u = sibling.get(k)
            if isinstance(u, str) and u.strip():
                return u.strip()
    # A suffix only names the unit of a MEASUREMENT. `note_nt` is prose that happens to end in a unit's letters,
    # and "sequence trimmed nt" is a card saying something the payload never said.
    inner = value.get("value") if isinstance(value, dict) else value
    if _prose(key) or not (isinstance(inner, (int, float)) and not isinstance(inner, bool)):
        return None
    for suffix, unit in UNIT_SUFFIX:
        if str(key).endswith(suffix):
            return unit
    return None


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
        # for "no quantum backend", an honest denial that a reader is not misled by (studio/claims.py), and turning
        # it into an absence would throw away the one field that says what really ran.
        return f"the payload holds {value!r}, with no reason given"
    if isinstance(value, list) and not value:
        # `ibm_job_ids: []` is not a null: the payload answered, and what it answered is "none of these".
        return "the payload holds an empty list"
    if isinstance(value, dict):
        has_slot = "value" in value or "null_reason" in value
        if has_slot and (value.get("value") is None or _bad_number(value.get("value"))):
            for k in ("null_reason", "reason", "disclaimer", "context", "note"):
                r = value.get(k)
                if isinstance(r, str) and r.strip():
                    return r.strip()
            return "null in the payload, with no reason given"
    return None


def _honest_value(value: Any) -> Any:
    """The number inside an honest-value wrapper, or the value itself."""
    if isinstance(value, dict) and ("value" in value or "units" in value or "unit" in value):
        inner = value.get("value")
        if _is_scalar(inner):
            return inner
    return value


def _walk(o: Any, path: str = "", depth: int = 0, limit: int = 8):
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


# --------------------------------------------------------------------------- block builders
def _facts_rows(obj: dict, prefix: str = "") -> tuple[list[dict], list[dict]]:
    """(rows, absent blocks) for one flat object. A missing field becomes an `absent` block, never a row."""
    rows: list[dict] = []
    gone: list[dict] = []
    for key, raw in obj.items():
        if key.endswith("_units") or key.endswith("_unit"):
            continue                                   # already carried as the unit of its own field
        label = f"{prefix}{key}"
        why = _honest_absent(raw)
        if why is not None:
            gone.append({"type": "absent", "what": label, "why": why})
            continue
        value = _honest_value(raw)
        if not _is_scalar(value):
            continue                                   # a container is not a fact; the caller recurses or tables it
        row: dict = {"label": label, "value": value}
        unit = _unit_for(key, raw, obj)
        if unit:
            row["unit"] = unit
        note = raw.get("note") or raw.get("interpretation") or raw.get("method") if isinstance(raw, dict) else None
        if isinstance(note, str) and note.strip():
            row["note"] = note.strip()[:300]
        rows.append(row)
    return rows, gone


def _table(items: list, label: str) -> list[dict]:
    """A list of uniform objects as a table, with one `absent` block per column that has a missing cell.

    A null in a cell is the empty cell rule 1 forbids, so the cell carries the word "absent" and the column says
    which rows and why. Grouping by column is what keeps this bounded: one block per column, not one per cell.
    """
    rows_in = [r for r in items if isinstance(r, dict)]
    columns: list[str] = []
    for r in rows_in:
        for k in r:
            if k not in columns and not (k.endswith("_units") or k.endswith("_unit")):
                columns.append(k)
    if not columns:
        return []
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
            v = _honest_value(raw)
            line.append(v if _is_scalar(v) else _short_json(v))
        body.append(line)
    # The unit belongs to the column, so it is said once, in the header.
    head = []
    for c in columns:
        sample = next((r[c] for r in kept if _honest_absent(r.get(c)) is None and c in r), None)
        unit = _unit_for(c, sample, rows_in[0] if rows_in else None)
        head.append(f"{c} ({unit})" if unit else c)
    out: list[dict] = [{"type": "table", "columns": head, "rows": body}]
    for c, hits in missing.items():
        whys = sorted({w for _, w in hits})
        where = "every row" if len(hits) == len(kept) else "rows " + ", ".join(str(i) for i, _ in hits[:20])
        out.append({"type": "absent", "what": f"{label}.{c}", "why": f"{where}: {'; '.join(whys)[:400]}"})
    if len(rows_in) > len(kept):
        out.append({"type": "text",
                    "text": f"{len(rows_in) - len(kept)} of {len(rows_in)} rows of {label} are not shown "
                            f"(the card holds at most {MAX_TABLE_ROWS})."})
    return out


def _short_json(v: Any) -> str:
    try:
        s = json.dumps(v, ensure_ascii=False, default=str, allow_nan=False)
    except ValueError:
        s = json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= 200 else s[:200] + f"… (+{len(s) - 200} characters)"


def _uniform_objects(v: Any) -> bool:
    """A list worth a table: at least two dict entries sharing at least one key, or one dict with several keys."""
    items = [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []
    if not items or len(items) < len(v) / 2:
        return False
    if len(items) == 1:
        return len(items[0]) > 1
    keys = set(items[0])
    return bool(keys & set(items[1]))


def _series_blocks(items: list, label: str) -> list[dict]:
    """A list of records with a numeric index and numeric metrics, as one series per metric."""
    rows = [r for r in items if isinstance(r, dict)]
    if len(rows) < 2:
        return []
    index = next((k for k in INDEX_KEYS if all(isinstance(r.get(k), (int, float)) and not isinstance(r.get(k), bool)
                                               and math.isfinite(r[k]) for r in rows)), None)
    if index is None:
        return []
    metrics = [k for k in rows[0]
               if k != index and not _prose(k)
               and all(isinstance(r.get(k), (int, float)) and not isinstance(r.get(k), bool)
                       and math.isfinite(r.get(k, float("nan"))) for r in rows)]
    out: list[dict] = []
    for metric in metrics[:MAX_SERIES_BLOCKS]:
        points = [[r[index], r[metric]] for r in rows[:MAX_SERIES_POINTS]]
        unit = _unit_for(metric, rows[0].get(metric), rows[0])
        out.append({"type": "series", "x_label": index,
                    "y_label": f"{metric} ({unit})" if unit else f"{label}.{metric}" if label else metric,
                    "points": points})
        if len(rows) > MAX_SERIES_POINTS:
            out.append({"type": "text",
                        "text": f"{len(rows) - MAX_SERIES_POINTS} of {len(rows)} points of {label}.{metric} are not "
                                f"shown (the card holds at most {MAX_SERIES_POINTS})."})
    if len(metrics) > MAX_SERIES_BLOCKS:
        out.append({"type": "text",
                    "text": f"{len(metrics) - MAX_SERIES_BLOCKS} further numeric series in {label} are not drawn: "
                            f"{', '.join(metrics[MAX_SERIES_BLOCKS:][:12])}."})
    return out


def _links(payload: Any) -> list[dict]:
    """Only an absolute http(s) URL is a link. A relative route (`poll_url: "/evil/route"`) is a fact, because
    nothing in Studio follows a route a payload names."""
    items: list[dict] = []
    seen: set[str] = set()
    for path, value in _walk(payload):
        key = _leaf_key(path)
        pairs: list[tuple[str, Any]] = []
        if key in ("files", "open_in") and isinstance(value, dict):
            pairs = list(value.items())
        elif key.endswith("_url") and isinstance(value, str):
            pairs = [(key, value)]
        for label, url in pairs:
            if isinstance(url, list):
                url = url[0] if url and isinstance(url[0], str) else None
            if isinstance(url, str) and HTTP_URL.match(url) and url not in seen:
                seen.add(url)
                items.append({"label": str(label), "url": url})
    return [{"type": "links", "items": items[:60]}] if items else []


def _text_block(s: str, label: str = "") -> dict:
    body = s if len(s) <= MAX_TEXT else s[:MAX_TEXT] + f"\n… {len(s) - MAX_TEXT} further characters are not shown."
    return {"type": "text", "text": f"{label}: {body}" if label else body}


# --------------------------------------------------------------------------- shape detectors
def _molecule_blocks(payload: Any, consumed: set[str]) -> list[dict]:
    """2D depiction and 3D structure, each only where the payload states the format it really is."""
    out: list[dict] = []
    b64_path, b64 = _first(payload, "image_base64", "png_base64", "image")
    fmt_path, fmt = _first(payload, "format", "image_format")
    smiles_path, smiles = _first(payload, "smiles", "canonical_smiles")
    if isinstance(b64, str) and b64.strip():
        fmt_name = str(fmt).strip().lower() if isinstance(fmt, str) else ""
        if fmt_name in ("png", "image/png") or b64.startswith("data:image/png"):
            block: dict = {"type": "molecule2d", "png_base64": b64}
            if isinstance(smiles, str) and smiles.strip():
                block["smiles"] = smiles.strip()
            out.append(block)
            consumed.update({p for p in (b64_path, fmt_path) if p})
        else:
            out.append({"type": "absent", "what": "molecule2d",
                        "why": f"the payload carries an image the card cannot claim is a PNG "
                               f"(format {fmt!r} at {fmt_path or 'nowhere'})"})
            out.append({"type": "facts", "rows": [{"label": "image bytes (base64)", "value": len(b64)}]})
            consumed.update({p for p in (b64_path, fmt_path) if p})
    elif isinstance(smiles, str) and smiles.strip():
        out.append({"type": "molecule2d", "smiles": smiles.strip()})

    sdf_path, sdf = _first(payload, "mol", "sdf", "mol_block", "mol_3d", "sdf_text")
    if isinstance(sdf, str) and sdf.strip():
        if "ATOM " in sdf or sdf.lstrip().startswith(("HEADER", "ATOM", "HETATM")):
            # PDB is not SDF: `structure_3d.pdb` is protein text and this block type carries SDF only. Carrying it
            # as molecule3d would hand the window a file it cannot open and call it a structure.
            out.append({"type": "absent", "what": "molecule3d",
                        "why": f"{sdf_path} holds PDB text, and this block type carries SDF only"})
            out.append(_text_block(sdf, sdf_path or "structure"))
        else:
            out.append({"type": "molecule3d", "sdf": sdf})
        if sdf_path:
            consumed.add(sdf_path)
    if smiles_path and any(b.get("type") in ("molecule2d", "molecule3d") for b in out):
        consumed.add(smiles_path)
    return out


def _admet_blocks(payload: Any, consumed: set[str]) -> list[dict]:
    """BioQL's ADMET answer: `raw_predictions` is a flat object of TDC endpoints, and a missing endpoint is a null
    (or a `float("nan")`, which `bionics_bridge` writes) that must be DRAWN as absent with its reason."""
    out: list[dict] = []
    for path, holder in _dicts_with(payload, "raw_predictions")[:4]:
        preds = holder.get("raw_predictions")
        if not isinstance(preds, dict) or not preds:
            continue
        label = f"{path}.raw_predictions" if path else "raw_predictions"
        out.append(_text_block(f"{len(preds)} endpoint(s) from {label}"))
        rows, gone = _facts_rows(preds, prefix="")
        if rows:
            out.append({"type": "facts", "rows": rows[:MAX_FACTS]})
            if len(rows) > MAX_FACTS:
                out.append({"type": "text", "text": f"{len(rows) - MAX_FACTS} further endpoints of {label} are not "
                                                    f"shown (the card holds at most {MAX_FACTS} facts)."})
        out.extend(gone)
        meta_rows, meta_gone = _facts_rows({k: v for k, v in holder.items()
                                            if k != "raw_predictions" and _is_scalar(v)})
        if meta_rows:
            out.append({"type": "facts", "rows": meta_rows[:MAX_FACTS]})
        out.extend(meta_gone)
        consumed.add(path or "raw_predictions")
    return out


def _catalogue_blocks(payload: Any, consumed: set[str]) -> list[dict]:
    """The motif catalogue: `{categories: {name: [...]}, total_motifs: n}`, and the flat `{motifs: {name: smarts}}`
    the tools panel reads."""
    out: list[dict] = []
    cat_path, cats = _first(payload, "categories")
    if isinstance(cats, dict) and cats:
        rows = [[name, len(items) if isinstance(items, (list, dict)) else 1] for name, items in cats.items()]
        out.append({"type": "table", "columns": ["category", "entries"], "rows": rows[:MAX_TABLE_ROWS]})
        if len(rows) > MAX_TABLE_ROWS:
            out.append({"type": "text", "text": f"{len(rows) - MAX_TABLE_ROWS} of {len(rows)} categories are not "
                                                f"shown (the card holds at most {MAX_TABLE_ROWS} rows)."})
        consumed.add(cat_path or "categories")
    mot_path, motifs = _first(payload, "motifs")
    if isinstance(motifs, dict) and motifs:
        rows = [[name, value if _is_scalar(value) else _short_json(value)] for name, value in motifs.items()]
        out.append({"type": "table", "columns": ["motif", "pattern"], "rows": rows[:MAX_TABLE_ROWS]})
        consumed.add(mot_path or "motifs")
    return out


def _job_blocks(payload: dict, consumed: set[str]) -> list[dict]:
    """A receipt: an id, a state, and the route BioQL says to poll — drawn as a fact, because Studio follows the
    allowlist's route and never one a body names."""
    rows, gone = _facts_rows({k: v for k, v in payload.items() if _is_scalar(v)})
    consumed.update(k for k, v in payload.items() if _is_scalar(v))
    return ([{"type": "facts", "rows": rows[:MAX_FACTS]}] if rows else []) + gone


# --------------------------------------------------------------------------- kind
def _kind_of(payload: Any) -> str:
    """The card's kind, from the shape alone."""
    if not isinstance(payload, dict):
        return "table" if _uniform_objects(payload) else "text"
    has = lambda *n: _first(payload, *n)[0] is not None
    if has("raw_predictions") or has("admet_classical", "admet_quantum", "admet"):
        return "admet"
    if has("categories", "motifs", "total_motifs"):
        return "catalogue"
    if has("binding_affinity_kcal_mol", "binding_affinity_kcal", "poses", "poses_generated", "docking"):
        return "docking"
    if has("structure_3d", "complex_structure_3d", "pdb_data", "complex_pdb", "plddt", "iptm"):
        return "structure"
    if has("mol", "sdf", "mol_3d", "mol_block", "image_base64", "png_base64"):
        return "molecule"
    if has("stages", "pipeline"):
        return "pipeline"
    if has("candidates", "screening", "hits", "ranked"):
        return "screening"
    if has("job_id", "poll_url") and not has("results", "result"):
        return "job"
    if any(_uniform_objects(v) for v in payload.values()):
        return "table"
    if all(_is_scalar(v) for v in payload.values()) and payload:
        return "table"          # a flat object draws as facts; the kind only says there is tabular data, not prose
    return "table" if payload else "text"


def _title_of(payload: Any, kind: str, tool: str | None) -> tuple[str, str | None]:
    """A name for the card, and a subtitle. The tool's name may reach the subtitle and NOTHING else: a tool that
    lies about what it is cannot change a kind or a block."""
    label = {"admet": "ADMET", "catalogue": "Catalogue", "docking": "Docking", "structure": "Structure",
             "molecule": "Molecule", "pipeline": "Pipeline", "screening": "Screening", "job": "Job",
             "table": "Result", "text": "Result"}[kind]
    name = None
    for key in ("pipeline", "pipeline_name", "target", "job_id", "smiles", "name", "title"):
        _, v = _first(payload, key)
        if isinstance(v, str) and v.strip():
            name = v.strip()[:80]
            break
    title = f"{label} · {name}" if name else label
    bits = [b for b in (tool.strip() if isinstance(tool, str) else None,
                        f"{len(payload)} top-level field(s)" if isinstance(payload, dict) else
                        f"{len(payload)} entries" if isinstance(payload, list) else None) if b]
    return title, " · ".join(bits) or None


# --------------------------------------------------------------------------- the remainder pass (rule 5)
def _remainder(payload: Any, consumed: set[str]) -> list[dict]:
    """Everything the detectors did not take, drawn: a flat object as facts, a uniform list as a table, anything
    else as text. This is what makes "nothing is lost" and "an unknown tool still carries its data" true rather
    than aspirational."""
    out: list[dict] = []
    if isinstance(payload, list):
        if _uniform_objects(payload):
            return _table(payload, "rows")
        return [_text_block(_short_json(payload))]
    if not isinstance(payload, dict):
        return [_text_block("" if payload is None else str(payload))]

    flat: dict[str, Any] = {}
    gone: list[dict] = []
    for key, value in payload.items():
        if key in consumed:
            continue
        if _is_scalar(value) or (isinstance(value, dict) and ("value" in value or "null_reason" in value)):
            flat[key] = value
        elif isinstance(value, list) and _uniform_objects(value):
            out.extend(_series_blocks(value, key))
            out.extend(_table(value, key))
        elif isinstance(value, list) and all(_is_scalar(x) for x in value):
            # The empty list is passed through untouched, so `_honest_absent` can say what it really is rather than
            # calling it a null.
            flat[key] = value if not value else ", ".join("" if x is None else str(x) for x in value[:40])
        elif isinstance(value, dict):
            # Two levels down, which is where BioQL puts the shapes that matter: `stages.<n>.<field>` and
            # `optimization.iterations[]`. A list nested under a dict is the common case (`iterations`,
            # `results`, `guides`), so it is drawn here rather than flattened into `a.b[0].c` rows.
            _, sub_gone = _facts_rows(value, prefix=f"{key}.")
            flat.update({f"{key}.{k}": v for k, v in value.items() if _is_scalar(v)})
            gone.extend(sub_gone)
            for k, v in value.items():
                if isinstance(v, list) and _uniform_objects(v):
                    out.extend(_series_blocks(v, f"{key}.{k}"))
                    out.extend(_table(v, f"{key}.{k}"))
            nested = {f"{key}.{k}": v for k, v in value.items() if isinstance(v, dict)}
            for npath, nval in list(nested.items())[:8]:
                flat.update({f"{npath}.{k}": v for k, v in nval.items() if _is_scalar(v)})
                for k, v in nval.items():
                    if isinstance(v, list) and _uniform_objects(v):
                        out.extend(_table(v, f"{npath}.{k}"))
        else:
            out.append(_text_block(_short_json(value), key))

    rows, more_gone = _facts_rows(flat)
    gone.extend(more_gone)
    if rows:
        out.insert(0, {"type": "facts", "rows": rows[:MAX_FACTS]})
        if len(rows) > MAX_FACTS:
            out.append({"type": "text", "text": f"{len(rows) - MAX_FACTS} of {len(rows)} facts are not shown "
                                                f"(the card holds at most {MAX_FACTS})."})
    out.extend(gone[:MAX_FACTS])
    if len(gone) > MAX_FACTS:
        out.append({"type": "text", "text": f"{len(gone) - MAX_FACTS} further absent field(s) are not listed."})
    return out


# --------------------------------------------------------------------------- the one public call
def card(payload: Any, tool: str | None = None, meta: dict | None = None) -> dict:
    """One BioQL result as a card spec. Never raises on a payload, and never returns a card with no blocks."""
    meta = meta if isinstance(meta, dict) else {}
    kind = _kind_of(payload)
    title, subtitle = _title_of(payload, kind, tool)

    consumed: set[str] = set()
    blocks: list[dict] = []
    if isinstance(payload, dict):
        blocks += _admet_blocks(payload, consumed) if kind == "admet" else []
        blocks += _catalogue_blocks(payload, consumed) if kind == "catalogue" else []
        blocks += _molecule_blocks(payload, consumed) if kind in ("molecule", "structure", "docking") else []
        blocks += _job_blocks(payload, consumed) if kind == "job" else []
        blocks += _links(payload)
    blocks += _remainder(payload, consumed)
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


def _provenance_of(payload: Any, given: Any) -> dict:
    """The card's provenance: this module's own walk of the payload, widened by what the caller already copied out
    of the same answer (the adapter records `quantum_backends` under that name; the card's key is `backends`).

    Union, never override: a job id or a backend one side saw and the other missed is kept, and the only way
    `is_real_hardware` becomes true here is a real boolean the payload wrote. Two sides that disagree are "mixed".
    """
    mine = provenance(payload)
    if not isinstance(given, dict):
        return mine
    flags = [f for f in (mine["is_real_hardware"], given.get("is_real_hardware")) if isinstance(f, bool)]
    jobs = list(mine["ibm_job_ids"])
    for j in (given.get("ibm_job_ids") or []):
        if isinstance(j, str) and j and j not in jobs:
            jobs.append(j)
    backends = list(mine["backends"])
    for b in (given.get("backends") or given.get("quantum_backends") or []):
        if isinstance(b, str) and b and b not in backends:
            backends.append(b)
    real = mine["is_real_hardware"]
    if real != "mixed" and given.get("is_real_hardware") == "mixed":
        real = "mixed"
    elif len(set(flags)) > 1:
        real = "mixed"
    elif flags:
        real = flags[0]
    return {"is_real_hardware": real, "ibm_job_ids": jobs, "backends": backends}
