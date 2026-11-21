"""Las moléculas que la corrida generó, dibujadas, con los números que deciden si sirven.

`candidates[]` es el único contenedor en el que las tarjetas de BioQL coinciden de verdad: `MoleculeStructuresCard`
lo recorre para dibujar, `MoleculeDossierCard` para los identificadores y `MoleculeQuantumCard` para el mapa
smiles→id. Y en una corrida real (PD-L1, `47507341-379`) cada candidato trae **`structure_svg` dentro**, además de
`structure_png_b64`: el dibujo ya viene hecho y no hay que pedirlo.

Hasta ahora Studio lo sacaba como una tabla genérica. Una tabla con una columna de SMILES es ilegible para la
persona que tiene que decidir cuál sintetizar: lo que se lee es la estructura, y al lado los dos o tres números
que importan.

**Tres reglas que no se cruzan:**

- *Una ausencia no se pinta como un cero.* `MoleculeStructuresCard` mete un candidato sin banda de síntesis en el
  cubo "unknown" y lo deja FUERA de `makeable`, así que un resultado sin datos de síntesis enseña un "0/N
  sintetizables" en rojo. Eso es una ausencia disfrazada de veredicto. Aquí los que no declaran banda se cuentan
  aparte y se dicen aparte.
- *El dibujo es de BioQL o no hay dibujo.* Si el candidato no trae `structure_svg` ni PNG, queda el SMILES y la
  ventana puede pedirle el dibujo a BioQL; lo que no se hace es inventar una estructura.
- *Un número honesto se lee entero.* `binding_affinity_kcal_honest` es `{value, null_reason, method, ...}`: si el
  valor es nulo, lo que se enseña es su razón, no un guion.
"""
from __future__ import annotations

import math
from typing import Any

# Doce, no veinticuatro: en la corrida real cada `structure_svg` pesa 95 KB, asi que una galeria de 24 son 2,3 MB
# de JSON por una sola tarjeta. Lo que pasa de ahi se dice y se queda en el resultado.
MAX_CANDIDATES = 12
CONTAINERS = ("candidates", "ranked_candidates", "hits", "molecules")

# Qué se enseña debajo del dibujo, en este orden. (clave(s), etiqueta, unidad)
NUMBERS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("composite_score", "score"), "puntuación", ""),
    (("drug_candidate_class",), "clase", ""),
    (("final_rank", "rank"), "puesto", ""),
    (("molecular_weight_da", "mw"), "peso", "Da"),
    (("qed",), "QED", ""),
    (("sa", "sascore", "sa_score"), "SAscore", ""),
    (("docking_kcal_mol",), "docking", "kcal/mol"),
    (("binding_affinity_kcal_honest", "binding_affinity_kcal"), "ΔG unión", "kcal/mol"),
    (("binding_confidence_iptm", "iptm"), "ipTM", ""),
    (("serum_half_life_hours",), "t½ suero", "h"),
    (("net_charge",), "carga", ""),
    (("aggregation_risk",), "agregación", ""),
    (("protease_resistance",), "proteasas", ""),
    (("manufacturability",), "fabricable", ""),
)


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def _honest(v: Any) -> tuple[Any, str | None]:
    """(valor, razón). Un campo `*_honest` es `{value, null_reason, ...}`: con el valor nulo, lo que vale es la
    razón, y un guion en su sitio sería esconderla."""
    if isinstance(v, dict) and ("value" in v or "null_reason" in v):
        val = v.get("value")
        if val is None or (isinstance(val, float) and not math.isfinite(val)):
            for k in ("null_reason", "reason", "disclaimer", "note"):
                r = v.get(k)
                if isinstance(r, str) and r.strip():
                    return None, r.strip()
            return None, "nulo en el resultado, sin razón"
        return val, None
    return v, None


def _fmt(v: Any, unit: str) -> str:
    if _num(v):
        s = f"{float(v):.4g}"
        return f"{s} {unit}".strip() if unit else s
    return str(v)


def _band(c: dict) -> str | None:
    """La banda de sintetizabilidad, si el candidato la declara. `None` NO es 'no sintetizable'."""
    s = c.get("synthesis")
    if isinstance(s, dict) and isinstance(s.get("band"), str) and s["band"].strip():
        return s["band"].strip()
    b = c.get("synthesis_band")
    return b.strip() if isinstance(b, str) and b.strip() else None


def _item(c: dict, i: int) -> dict | None:
    if not isinstance(c, dict):
        return None
    label = str(c.get("name") or c.get("id") or c.get("candidate_id") or f"candidato {i + 1}")
    item: dict = {"label": label}

    svg = c.get("structure_svg") or c.get("svg")
    png = c.get("structure_png_b64") or c.get("structure_png") or c.get("image_base64")
    if isinstance(svg, str) and svg.strip().startswith("<"):
        item["svg"] = svg
    elif isinstance(png, str) and png.strip():
        item["png_base64"] = png.strip()
    smiles = c.get("smiles") or c.get("canonical_smiles")
    if isinstance(smiles, str) and smiles.strip():
        item["smiles"] = smiles.strip()
    seq = c.get("sequence")
    if isinstance(seq, str) and seq.strip() and "svg" not in item and "png_base64" not in item:
        item["smiles"] = seq.strip()          # un péptido sin dibujo: su secuencia ES su identidad

    rows, gaps = [], []
    for keys, lab, unit in NUMBERS:
        for k in keys:
            if k not in c:
                continue
            val, why = _honest(c[k])
            if why:
                gaps.append(f"{lab}: {why}")
            elif val is not None and val != "":
                rows.append({"label": lab, "value": _fmt(val, unit)})
            break
    banda = _band(c)
    if banda:
        rows.append({"label": "síntesis", "value": banda})
    if rows:
        item["rows"] = rows[:10]
    if gaps:
        item["gaps"] = gaps[:4]
    return item if ("svg" in item or "png_base64" in item or rows or "smiles" in item) else None


def candidate_blocks(payload: Any, consumed: set[str]) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    found: list[tuple[str, list]] = []
    for base, node in (("", payload), ("result", payload.get("result"))):
        if not isinstance(node, dict):
            continue
        for name in CONTAINERS:
            v = node.get(name)
            if isinstance(v, list) and v and any(isinstance(x, dict) for x in v):
                found.append((f"{base}.{name}" if base else name, v))
                break
    if not found:
        return []

    out: list[dict] = []
    for path, lst in found[:1]:          # un contenedor por tarjeta: dos galerías es una lista partida en dos
        items = [it for it in (_item(c, i) for i, c in enumerate(lst[:MAX_CANDIDATES])) if it]
        if not items:
            continue
        # Una galeria existe cuando hay ALGO QUE MIRAR. Una lista de `{id, score}` sin estructura ni SMILES son
        # numeros, y doscientas baldosas vacias se leen peor que una tabla -- que ademas ya sabe recortar, decir
        # cuantas filas dejo fuera y marcar la celda que falta. En ese caso esto se aparta.
        if not any(("svg" in it or "png_base64" in it or "smiles" in it) for it in items):
            continue
        con_dibujo = sum(1 for it in items if "svg" in it or "png_base64" in it)
        bandas = [_band(c) for c in lst[:MAX_CANDIDATES] if isinstance(c, dict)]
        sin_banda = sum(1 for b in bandas if b is None)
        out.append({"type": "gallery", "items": items, "count": len(lst)})
        if len(lst) > MAX_CANDIDATES:
            out.append({"type": "text",
                        "text": f"{len(lst) - MAX_CANDIDATES} de {len(lst)} candidatos no se dibujan aquí; "
                                f"están todos en el resultado"})
        if con_dibujo < len(items):
            out.append({"type": "absent", "what": f"{len(items) - con_dibujo} dibujo(s) de {len(items)}",
                        "why": "esos candidatos no traen `structure_svg` ni PNG; la ventana puede pedirle el "
                               "dibujo a BioQL desde su SMILES, y lo que no hace es inventar una estructura"})
        if sin_banda:
            # La trampa de MoleculeStructuresCard, al revés: no declarar banda no es "no sintetizable".
            out.append({"type": "absent", "what": f"síntesis de {sin_banda} candidato(s)",
                        "why": "no declaran banda de sintetizabilidad; se cuentan aparte y no como "
                               "'no sintetizables', que es una ausencia disfrazada de veredicto"})
        huecos = [g for it in items for g in it.get("gaps", [])]
        if huecos:
            out.append({"type": "absent", "what": "números que el resultado nombra y no midió",
                        "why": " · ".join(huecos[:6])})
        consumed.add(path)
    return out
