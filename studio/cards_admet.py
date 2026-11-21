"""ADMET: la predicción, y si el modelo tiene derecho a ella.

BioQL no devuelve doce números: devuelve doce números **con predicción conformal calibrada**. Cada endpoint trae
un `prediction_set`, un `q_hat`, una cobertura objetivo (0,9) y la cobertura empírica medida sobre una partición
que su propia fuente llama *"provably disjoint from training"*. Y trae el AUC del modelo sobre el test, con n.

Eso cambia qué hay que dibujar. Un número sin su conjunto conformal es una opinión; con él se sabe algo mucho
más útil: **cuándo el modelo se niega a decidir**. Un `prediction_set` con las dos clases dentro (`[0, 1]`) es
el modelo diciendo "a esta molécula no llego", y leer su `p_positive` como si fuera una predicción es
exactamente el defecto de los tres estados — un valor plausible en lugar de pararse — pero dentro de la ciencia.
Así que la fila lo dice en su propia columna, no en una nota al pie.

**Los dos brazos.** El resultado trae `admet_classical` y `admet_quantum`. Medidos sobre una corrida real
(job 6f02faa5-e80, 2026-11-20): **idénticos en los 17 campos**, con `ran_quantum: False` y un `quantum_engine`
que dice "classical". Son el mismo cálculo copiado bajo otro nombre. Enseñarlos en dos columnas sería presentar
una medida como si fueran dos métodos, que es la trampa del brazo flojo de CLAUDE.md con la etiqueta al revés.
Cuando coinciden se dibuja UNO y se dice por qué; cuando difieren se dibujan los dos y se marcan las filas que
cambian, que es el único caso en que la comparación significa algo.
"""
from __future__ import annotations

import math
from typing import Any

GROUPS = ("absorption", "distribution", "metabolism", "excretion", "toxicity")
GROUP_ES = {"absorption": "absorción", "distribution": "distribución", "metabolism": "metabolismo",
            "excretion": "excreción", "toxicity": "toxicidad"}
MAX_ROWS = 60


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def _fmt(v: Any, unit: str = "") -> Any:
    if _num(v):
        f = float(v)
        s = f"{f:.4g}"
        return f"{s} {unit}".strip() if unit else s
    return v if v is not None else "—"


def _conformal_call(conf: dict) -> tuple[str, str]:
    """(veredicto, por qué). El conjunto manda sobre la etiqueta: una etiqueta bonita con las dos clases dentro
    sigue siendo una negativa a decidir."""
    if not isinstance(conf, dict) or not conf:
        return "sin calibrar", "el endpoint no trae conjunto conformal"
    ps = conf.get("prediction_set")
    if isinstance(ps, list):
        if len(ps) > 1:
            return "AMBIGUO", (f"el conjunto conformal contiene {ps}: a {conf.get('target_coverage', '?')} de "
                               f"cobertura el modelo no distingue, y su p={conf.get('p_positive')} no es una "
                               f"predicción")
        if len(ps) == 0:
            return "AMBIGUO", "el conjunto conformal salió vacío: ninguna clase alcanza la cobertura pedida"
    if conf.get("ambiguous") is True:
        return "AMBIGUO", "el motor lo marca ambiguo"
    lab = str(conf.get("interpretation") or "").strip()
    return (lab or "con confianza"), ""


def _endpoint_rows(arm: dict) -> tuple[list[list], list[str], dict]:
    """Una fila por endpoint, los ambiguos aparte, y la calibración que comparten."""
    rows: list[list] = []
    ambiguos: list[str] = []
    cal: dict = {}
    for g in GROUPS:
        d = arm.get(g)
        if not isinstance(d, dict):
            continue
        for name, e in d.items():
            if not isinstance(e, dict):
                continue
            val = e.get("value")
            unit = str(e.get("unit") or "")
            label = str(e.get("label") or name)
            interp = e.get("interpretation")
            v = e.get("validation") if isinstance(e.get("validation"), dict) else {}
            conf = v.get("conformal") if isinstance(v.get("conformal"), dict) else {}
            verdict, why = _conformal_call(conf)
            if verdict == "AMBIGUO":
                ambiguos.append(f"{label} — {why}")
            meas = v.get("measured") if isinstance(v.get("measured"), dict) else {}
            mets = meas.get("all_metrics") if isinstance(meas.get("all_metrics"), dict) else {}
            auc = mets.get("roc_auc")
            rows.append([GROUP_ES.get(g, g), label, _fmt(val, unit),
                         str(interp) if interp is not None else "—",
                         verdict, _fmt(auc) if _num(auc) else "—"])
            if not cal and conf:
                cal = {"objetivo": conf.get("target_coverage"),
                       "empírica": conf.get("empirical_coverage_test"),
                       "fuente": conf.get("calibration_source"),
                       "método": conf.get("method")}
    return rows, ambiguos, cal


def _same(a: Any, b: Any) -> bool:
    try:
        import json
        return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
    except Exception:
        return a == b


def admet_blocks(payload: Any, consumed: set[str]) -> list[dict]:
    """El panel ADMET de un resultado de BioQL, con su calibración y sus negativas a decidir."""
    if not isinstance(payload, dict):
        return []
    # El resultado real envuelve en `results: [ {admet_classical, admet_quantum, smiles, ran_quantum, ...} ]`.
    # Las rutas se construyen EXACTAMENTE como las escribe `_walk` de cards.py -- `a.b[0].c`, sin prefijo
    # inventado -- porque son las que luego se comparan contra `consumed`. Una ruta con otro formato no marca
    # nada y el recorrido generico vuelve a volcar el mismo arbol debajo de la tarjeta.
    holders: list[tuple[str, dict]] = []
    for base, node in (("", payload), ("result", payload.get("result"))):
        if not isinstance(node, dict):
            continue
        if isinstance(node.get("admet_classical"), dict) or isinstance(node.get("admet_quantum"), dict):
            holders.append((base, node))
        rs = node.get("results")
        if isinstance(rs, list):
            for i, r in enumerate(rs):
                if isinstance(r, dict) and (isinstance(r.get("admet_classical"), dict)
                                            or isinstance(r.get("admet_quantum"), dict)):
                    holders.append((f"{base}.results[{i}]" if base else f"results[{i}]", r))
    if not holders:
        return []

    out: list[dict] = []
    for where, h in holders[:6]:
        classical = h.get("admet_classical") if isinstance(h.get("admet_classical"), dict) else {}
        quantum = h.get("admet_quantum") if isinstance(h.get("admet_quantum"), dict) else {}
        smiles = str(h.get("smiles") or "").strip()
        if smiles:
            out.append({"type": "molecule2d", "smiles": smiles})

        iguales = bool(classical) and bool(quantum) and _same(classical, quantum)
        ran_q = h.get("ran_quantum")
        if iguales:
            # Lo importante va arriba, no al pie: no son dos métodos.
            out.append({"type": "status", "word": "UN BRAZO", "tone": "warn",
                        "where": "`admet_classical` y `admet_quantum` son idénticos campo a campo: es el mismo "
                                  "cálculo bajo dos nombres, no dos métodos"})
            motivo = []
            if ran_q is False:
                motivo.append("`ran_quantum: false`")
            qe = str((classical or quantum).get("quantum_engine") or "")
            if qe:
                motivo.append(f"`quantum_engine: {qe[:60]}`")
            if motivo:
                out.append({"type": "text", "text": "Lo que BioQL dice de ese brazo: " + " · ".join(motivo)})
        elif classical and quantum:
            out.append({"type": "status", "word": "DOS BRAZOS", "tone": "good",
                        "where": "los dos brazos difieren: la comparación significa algo"})

        arm = classical or quantum
        rows, ambiguos, cal = _endpoint_rows(arm)
        if rows:
            out.append({"type": "table",
                        "columns": ["grupo", "endpoint", "valor", "lectura", "conformal", "AUC test"],
                        "rows": rows[:MAX_ROWS]})
        if ambiguos:
            # Una ausencia de decisión no es una decisión. Se nombran una por una.
            out.append({"type": "absent", "what": f"{len(ambiguos)} endpoint(s) sin decisión conformal",
                        "why": " · ".join(ambiguos[:6])})
        if cal.get("objetivo") is not None or cal.get("empírica") is not None:
            filas = [{"label": "cobertura objetivo", "value": _fmt(cal.get("objetivo"))},
                     {"label": "cobertura empírica medida", "value": _fmt(cal.get("empírica"))}]
            if cal.get("fuente"):
                filas.append({"label": "calibrado contra", "value": str(cal["fuente"])[:140]})
            out.append({"type": "facts", "rows": filas})

        if not iguales and classical and quantum:
            rc, _, _ = _endpoint_rows(classical)
            rq, _, _ = _endpoint_rows(quantum)
            porq = {r[1]: r[2] for r in rq}
            dif = [[r[0], r[1], r[2], porq.get(r[1], "—")] for r in rc if porq.get(r[1], "—") != r[2]]
            if dif:
                out.append({"type": "table", "columns": ["grupo", "endpoint", "clásico", "cuántico"],
                            "rows": dif[:MAX_ROWS]})
        # El subarbol ENTERO: ya esta dibujado arriba, con sus unidades y su conformal. Dejarlo sin marcar hacia
        # que el recorrido generico lo volcara otra vez endpoint por endpoint -- 27 bloques repetidos medidos.
        pre = f"{where}." if where else ""
        for arm_key in ("admet_classical", "admet_quantum"):
            if not isinstance(h.get(arm_key), dict):
                continue
            root = f"{pre}{arm_key}"
            consumed.add(root)
            for sub, val in h[arm_key].items():
                consumed.add(f"{root}.{sub}")
                if isinstance(val, dict):
                    for leaf in val:
                        consumed.add(f"{root}.{sub}.{leaf}")
        if smiles:
            consumed.add(f"{pre}smiles")
        for k in ("ran_quantum", "engine", "method", "molecule_id", "status"):
            consumed.add(f"{pre}{k}")
    return out
