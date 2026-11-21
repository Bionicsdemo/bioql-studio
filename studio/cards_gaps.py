"""`available: false` — el hueco que BioQL declara, en todo el resultado.

Es la convención que más repiten las tarjetas de BioQL. Seis de ellas (`ResultContractCard`, `ClinicalRiskCard`,
`DevelopabilityCard`, `InteractionsCard`, `BindingPocketsCard`, `LeadOptCard`) se abren o se callan según este
único campo, y el motor lo escribe en cualquier rincón del árbol.

Hasta ahora Studio lo sacaba como dos filas — `available: False` y `reason: "..."` — entre varios cientos. Y lo
que dicen esas razones, medido sobre la corrida PD-L1 real (`47507341-379`), no es contabilidad:

    hybrid_scoring.binding          no quantum or AI binding component present this run
    trained_generator               promoted checkpoint not found: /checkpoints/bioql_esm2_hla_a
    geneformer_xcheck               ModuleNotFoundError: No module named 'geneformer_validator'
    singlecell_xcheck               ModuleNotFoundError: No module named 'bionemo_singlecell'

Es decir: el término de unión no entró en la puntuación, el generador entrenado no se cargó, y dos validaciones
cruzadas no corrieron. Quien mira un 0,752 necesita eso arriba, no en la fila 180.

`available: true` no genera bloque: lo que dice ya lo dicen los datos que trae al lado, y una fila "disponible:
sí" es ruido. Pero la clave se marca consumida igual, para que no vuelva a salir como dato suelto.
"""
from __future__ import annotations

import re
from typing import Any

# El orden del frontend: la razón primero, la nota después. `reason_en` y `note_en` existen porque BioQL escribe
# algunas en inglés y otras en español, y la que haya es la que vale.
WHY_KEYS = ("reason", "reason_en", "reason_es", "error", "why", "note", "note_en", "note_es",
            "honest_note", "caveat", "disclaimer", "detail")
MAX_GAPS = 14
MAX_DEPTH = 7

# ---------------------------------------------------------------- de que clase es el hueco
#
# Un hueco declarado no es un punto final: es una frase que dice POR QUE no hay numero, y de esa frase se deduce
# que haria falta para que lo hubiera. Clasificado sobre las 26 razones distintas de una corrida PD-L1 real:
#
#   ENTRADA  BioQL sabe calcularlo y le falta un dato -- "need receptor sequence to fold the complex",
#            "promoted checkpoint not found", "No module named 'geneformer_validator'". Son CINCO de las 26, y
#            son las valiosas: conseguir ese dato y volver a llamar devuelve un numero DE BIOQL, medido.
#   MODELO   BioQL se niega a predecir porque no tiene con que -- "no validated per-molecule dose model",
#            "clinical probability-of-success not predicted here". Aqui un numero inventado es exactamente el
#            defecto que este proyecto ya envio una vez (51,2 / 49,3 / 48,8 mg para la misma entrada). Lo que
#            cabe es conocimiento EXTERNO y ETIQUETADO, nunca una prediccion por molecula.
#   OTRO     el resto.
#
# La clase se deduce del texto de BioQL y de nada mas. No se adivina que dato falta: si BioQL dice "need X", se
# cita "X" tal cual; si no lo dice, no se inventa.
NEEDS_INPUT = re.compile(
    r"\bneed(?:s)? (?:a |an |the )?(?P<que>[^.;,]{3,80})"
    r"|No module named '(?P<mod>[^']+)'"
    r"|(?P<ckpt>checkpoint not found[^.;]*)"
    r"|requires? (?:a |an |the )?(?P<req>[^.;,]{3,80})", re.I)
NO_MODEL = re.compile(r"no validated|no quantitative|not predicted here|no [a-z/ -]{0,24}model\b", re.I)


def classify(why: str) -> tuple[str, str | None]:
    """(clase, lo que falta segun BioQL). La segunda es None cuando BioQL no lo dice."""
    w = str(why or "")
    m = NEEDS_INPUT.search(w)
    if m:
        falta = m.group("que") or m.group("mod") or m.group("ckpt") or m.group("req")
        return "entrada", (falta.strip() if falta else None)
    if NO_MODEL.search(w):
        return "modelo", None
    return "otro", None


def _why(d: dict) -> str:
    for k in WHY_KEYS:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return "BioQL lo declara no disponible y no dice por qué"


def _walk(o: Any, path: str = "", depth: int = 0):
    yield path, o
    if depth >= MAX_DEPTH:
        return
    if isinstance(o, dict):
        for k, v in o.items():
            yield from _walk(v, f"{path}.{k}" if path else str(k), depth + 1)
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from _walk(v, f"{path}[{i}]", depth + 1)


def gap_blocks(payload: Any, consumed: set[str]) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    gaps: list[tuple[str, str]] = []
    for path, node in _walk(payload):
        if not isinstance(node, dict) or "available" not in node:
            continue
        av = node.get("available")
        if av is not False:
            if av is True:
                consumed.add(f"{path}.available" if path else "available")
            continue
        why = _why(node)
        # Lo que se consume es el `available` y la razón que se acaba de enseñar; el resto del objeto sigue
        # siendo suyo y se dibuja como siempre.
        consumed.add(f"{path}.available" if path else "available")
        for k in WHY_KEYS:
            if isinstance(node.get(k), str) and node[k].strip() == why:
                consumed.add(f"{path}.{k}" if path else k)
                break
        gaps.append((path or "el resultado", why))

    if not gaps:
        return []
    out = []
    for p, w in gaps[:MAX_GAPS]:
        b = {"type": "absent", "what": p, "why": w}
        clase, falta = classify(w)
        if clase != "otro":
            b["gap"] = clase
        if falta:
            b["fix"] = falta
        out.append(b)
    if len(gaps) > MAX_GAPS:
        out.append({"type": "text",
                    "text": f"{len(gaps) - MAX_GAPS} hueco(s) declarado(s) más, no mostrados aquí"})
    return out
