"""Lo que el sobre de BioQL dice y `cards.py` todavía no leía.

**Lo que este módulo NO hace, y por qué importa.** La primera versión dibujaba el sobre entero — estructura,
gráficos, enlaces — y al pasar la suite cayeron cinco pruebas que ya existían: `cards.py` llevaba tiempo sacando
la estructura de `visualization.molecular.embed_3dmol` (con papel `protein` para un PDB) y los gráficos de
`visualization.charts[]` (con su `url`, y **refusando** una `//otro/host`). Era trabajo duplicado, peor probado,
y con un agujero de seguridad que el código viejo no tenía. Así que este módulo quedó recortado a lo que de
verdad faltaba, y la estructura y los gráficos siguen siendo de `cards.py`.

Lo que faltaba, leído de `_attach_visualizations` (monolito 32343) y de `PipelineResultCards.tsx`:

- **`card.verdict`** — si la molécula pasa o no. Era una fila más entre doscientas.
- **`card.metrics` y `card.lead`** — los números que el motor eligió destacar, y la molécula líder.
- **`card.comparison_axes[].aligned`** — un eje marcado como NO emparejado significa que los dos brazos no
  corrieron con la misma configuración. En este proyecto una ventaja entera resultó ser el valor por defecto de
  disparos de un brazo (CLAUDE.md, trampas de comparación). Va en su propia columna de la tabla.
- **`molecular.chain_labels`** — qué cadena es VH, cuál VL y cuál el antígeno. El propio comentario de BioQL al
  construirlo dice que sin esto el complejo "renders as one anonymous blob".
- **`molecular.available: False`** — con su `reason` ("server-side rendering disabled", "exceeded 600s"). Eso es
  información sobre la corrida, no un hueco.
- **`downloads.files`** — los ficheros que la corrida dejó.

Las URLs pasan por la MISMA regla que `cards.py`: una ruta del mismo origen o una http(s) absoluta, nunca una
`//host` ni un `javascript:`. Se importa de allí dentro de la función para no cerrar un círculo de imports.
"""
from __future__ import annotations

from typing import Any

MAX_METRICS = 40
MAX_AXES = 24
MAX_FILES = 24


def _s(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _links_of(pairs: list[tuple[str, Any]]) -> list[dict]:
    """Enlaces, con la regla de `cards.py` y ninguna otra."""
    from .cards import HTTP_URL, SAME_ORIGIN_PATH
    out, seen = [], set()
    for label, url in pairs:
        u = _s(url)
        if not u or u in seen:
            continue
        if not (HTTP_URL.match(u) or SAME_ORIGIN_PATH.match(u)):
            continue
        seen.add(u)
        out.append({"label": str(label), "url": u})
    return out


def envelope_of(payload: Any) -> tuple[str | None, dict | None]:
    """El sobre, en la raíz o bajo `result` — los dos sitios donde la UI de BioQL lo busca."""
    if not isinstance(payload, dict):
        return None, None
    for path, holder in (("visualization", payload),
                         ("result.visualization",
                          payload.get("result") if isinstance(payload.get("result"), dict) else None)):
        if isinstance(holder, dict):
            v = holder.get("visualization")
            if isinstance(v, dict) and v:
                return path, v
    return None, None


def _card_blocks(card: dict, where: str, consumed: set[str]) -> list[dict]:
    out: list[dict] = []

    if "verdict" in card:
        verdict = card.get("verdict")
        if _s(verdict):
            word = _s(verdict).upper()
            tone = ("good" if word in ("PASS", "GO", "APPROVED", "CLEAR", "OK") else
                    "bad" if word in ("FAIL", "NO-GO", "STOP", "BLOCKED", "REJECT") else "plain")
            b = {"type": "status", "word": word, "tone": tone}
            note = _s(card.get("verdict_note"))
            if note:
                b["where"] = note
            out.append(b)
        else:
            # La clave puesta y sin valor no se lee como "pasa": es que nadie lo dictaminó.
            out.append({"type": "absent", "what": f"{where}.verdict",
                        "why": "BioQL trae la clave del veredicto sin valor: no se lee como aprobado"})
        consumed.add(f"{where}.verdict")
        consumed.add(f"{where}.verdict_note")

    rows = [{"label": k, "value": v} for k, v in (("título", _s(card.get("title"))),
                                                  ("subtítulo", _s(card.get("subtitle"))),
                                                  ("modalidad", _s(card.get("modality")))) if v]
    if rows:
        out.append({"type": "facts", "rows": rows})
    for k in ("title", "subtitle", "modality", "status"):
        consumed.add(f"{where}.{k}")

    lead = card.get("lead")
    if isinstance(lead, dict):
        seq = _s(lead.get("sequence"))
        if seq:
            out.append({"type": "facts", "rows": [{"label": "secuencia", "value": seq},
                                                  {"label": "largo", "value": len(seq)}]})
            consumed.add(f"{where}.lead.sequence")
        scores = lead.get("scores")
        if isinstance(scores, dict) and scores:
            out.append({"type": "table", "columns": ["puntuación", "valor"],
                        "rows": [[str(k), v] for k, v in list(scores.items())[:MAX_METRICS]]})
            consumed.add(f"{where}.lead.scores")

    metrics = card.get("metrics")
    if isinstance(metrics, dict) and metrics:
        got = [[str(k), v] for k, v in list(metrics.items())[:MAX_METRICS] if v is not None]
        sin = [str(k) for k, v in metrics.items() if v is None]
        if got:
            out.append({"type": "table", "columns": ["métrica", "valor"], "rows": got})
        if sin:
            out.append({"type": "absent", "what": f"{where}.metrics",
                        "why": f"{', '.join(sin[:8])} — BioQL las nombró y no las midió"})
        consumed.add(f"{where}.metrics")

    axes = card.get("comparison_axes")
    if isinstance(axes, list) and axes:
        filas, sueltos = [], []
        for a in axes[:MAX_AXES]:
            if not isinstance(a, dict):
                continue
            nombre = _s(a.get("axis")) or "—"
            al = a.get("aligned")
            marca = ("sí" if al is True or _s(al).lower() in ("true", "yes", "sí")
                     else "NO EMPAREJADO" if al is not None else "no dicho")
            if marca == "NO EMPAREJADO":
                sueltos.append(nombre)
            filas.append([nombre, a.get("peptide"), a.get("antibody"),
                          _s(a.get("calibration")) or "—", marca])
        if filas:
            out.append({"type": "table",
                        "columns": ["eje", "péptido", "anticuerpo", "calibración", "emparejado"],
                        "rows": filas})
        if sueltos:
            out.append({"type": "absent", "what": "la comparación",
                        "why": f"{', '.join(sueltos[:6])} — BioQL marca estos ejes como no emparejados: los dos "
                               f"brazos no corrieron con la misma configuración, así que la diferencia no es "
                               f"atribuible al método"})
        consumed.add(f"{where}.comparison_axes")
    return out


def _molecular_blocks(mol: dict, where: str, consumed: set[str]) -> list[dict]:
    """Sólo lo que `cards.py` no saca ya: por qué no hay estructura, qué es cada cadena, y el aviso."""
    out: list[dict] = []
    if mol.get("available") is False:
        why = _s(mol.get("reason")) or _s(mol.get("error")) or "BioQL no dijo por qué"
        out.append({"type": "absent", "what": f"{where} (estructura)", "why": why})
        for k in ("available", "reason", "error"):
            consumed.add(f"{where}.{k}")
        return out

    rows = [{"label": k, "value": v} for k, v in (("qué es", _s(mol.get("title"))),
                                                  ("aviso", _s(mol.get("note")))) if v]
    if rows:
        out.append({"type": "facts", "rows": rows})
        consumed.add(f"{where}.title")
        consumed.add(f"{where}.note")

    labels = mol.get("chain_labels")
    if isinstance(labels, dict) and labels:
        out.append({"type": "table", "columns": ["cadena", "qué es"],
                    "rows": [[str(k), str(v)] for k, v in sorted(labels.items())]})
        consumed.add(f"{where}.chain_labels")

    enlaces = _links_of([("abrir en " + str(app), url)
                         for app, url in (mol.get("open_in") or {}).items()
                         if isinstance(mol.get("open_in"), dict)])
    if enlaces:
        out.append({"type": "links", "items": enlaces[:MAX_FILES]})
        consumed.add(f"{where}.open_in")
    return out


def _download_blocks(dl: dict, where: str, consumed: set[str]) -> list[dict]:
    files = dl.get("files")
    if not isinstance(files, dict) or not files:
        return []
    items = _links_of(list(files.items())[:MAX_FILES])
    out: list[dict] = []
    if items:
        out.append({"type": "links", "items": items})
        consumed.add(f"{where}.files")
    if len(files) > MAX_FILES:
        out.append({"type": "text", "text": f"{len(files) - MAX_FILES} fichero(s) más en {where}.files"})
    return out


def viz_blocks(payload: Any, consumed: set[str]) -> list[dict]:
    where, env = envelope_of(payload)
    if env is None:
        return []
    out: list[dict] = []
    card = env.get("card")
    if isinstance(card, dict) and card:
        out += _card_blocks(card, f"{where}.card", consumed)
    mol = env.get("molecular")
    if isinstance(mol, dict) and mol:
        out += _molecular_blocks(mol, f"{where}.molecular", consumed)
    dl = env.get("downloads")
    if isinstance(dl, dict) and dl:
        out += _download_blocks(dl, f"{where}.downloads", consumed)
    return out
