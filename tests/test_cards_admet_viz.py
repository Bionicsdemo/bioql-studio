"""ADMET con su calibración, y lo que el sobre de BioQL dice que `cards.py` no leía.

El fixture `fixtures/admet_real.json` es un recorte de una corrida de verdad (job 6f02faa5-e80, aspirina,
2026-11-20): seis endpoints con su conjunto conformal, tal como llegaron por HTTP. No está inventado, y por eso
sostiene la afirmación más incómoda de este fichero — que los dos brazos del ADMET son el mismo cálculo.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from studio import cards

FIX = json.loads((Path(__file__).resolve().parent / "fixtures" / "admet_real.json").read_text())


def blocks(payload, tool="admet_standalone"):
    return cards.card(payload, tool=tool)["blocks"]


def of(payload, kind, tool="admet_standalone"):
    return [b for b in blocks(payload, tool) if b.get("type") == kind]


# ------------------------------------------------------------------ los dos brazos
def test_los_dos_brazos_identicos_se_dicen_como_uno():
    """EL test. En la corrida real `admet_classical` y `admet_quantum` son idénticos campo a campo, con
    `ran_quantum: false`. Dos columnas serían una medida presentada como dos métodos — la trampa del brazo
    flojo de CLAUDE.md con la etiqueta al revés."""
    s = [b for b in of(FIX, "status") if b["word"] == "UN BRAZO"]
    assert s, "no se dijo que los dos brazos son el mismo cálculo"
    assert "idénticos" in s[0]["where"]
    # y la prueba que lo respalda viaja con él
    txt = " ".join(b.get("text", "") for b in of(FIX, "text"))
    assert "ran_quantum: false" in txt


def test_dos_brazos_que_de_verdad_difieren_se_comparan():
    p = json.loads(json.dumps(FIX))
    p["results"][0]["admet_quantum"]["toxicity"]["ames"]["value"] = 0.9
    p["results"][0]["ran_quantum"] = True
    assert [b for b in of(p, "status") if b["word"] == "DOS BRAZOS"], "una diferencia real no se señaló"
    difs = [b for b in of(p, "table") if b.get("columns") == ["grupo", "endpoint", "clásico", "cuántico"]]
    assert difs and difs[0]["rows"], "no se listaron las filas que cambian"


# ------------------------------------------------------------------ el conjunto conformal manda
def test_un_conjunto_con_las_dos_clases_es_una_negativa_a_decidir():
    """Un `prediction_set` de [0, 1] es el modelo diciendo que no llega. Su `p_positive` no es una predicción, y
    dibujarlo como tal es el defecto de los tres estados dentro de la ciencia."""
    p = json.loads(json.dumps(FIX))
    hia = p["results"][0]["admet_classical"]["absorption"]["hia"]
    hia["validation"]["conformal"]["prediction_set"] = [0, 1]
    t = [b for b in of(p, "table") if "conformal" in (b.get("columns") or [])][0]
    fila = [r for r in t["rows"] if "Intestinal" in r[1]][0]
    assert fila[4] == "AMBIGUO", fila
    aus = " ".join(b["why"] for b in of(p, "absent"))
    assert "no distingue" in aus and "no es una" in aus


def test_un_conjunto_vacio_tambien_es_una_negativa():
    p = json.loads(json.dumps(FIX))
    p["results"][0]["admet_classical"]["absorption"]["hia"]["validation"]["conformal"]["prediction_set"] = []
    t = [b for b in of(p, "table") if "conformal" in (b.get("columns") or [])][0]
    assert [r for r in t["rows"] if r[4] == "AMBIGUO"], "un conjunto vacío pasó como decisión"


def test_la_bandera_ambiguous_del_motor_se_respeta_aunque_el_conjunto_parezca_claro():
    p = json.loads(json.dumps(FIX))
    c = p["results"][0]["admet_classical"]["toxicity"]["ames"]["validation"]["conformal"]
    c["prediction_set"] = [0]
    c["ambiguous"] = True
    t = [b for b in of(p, "table") if "conformal" in (b.get("columns") or [])][0]
    assert [r for r in t["rows"] if r[1].startswith("Ames") and r[4] == "AMBIGUO"]


def test_un_endpoint_sin_conformal_no_se_lee_como_confiable():
    p = json.loads(json.dumps(FIX))
    del p["results"][0]["admet_classical"]["toxicity"]["ames"]["validation"]["conformal"]
    t = [b for b in of(p, "table") if "conformal" in (b.get("columns") or [])][0]
    fila = [r for r in t["rows"] if r[1].startswith("Ames")][0]
    assert fila[4] == "sin calibrar", fila


def test_la_calibracion_viaja_con_la_tabla():
    """Una cobertura del 0,9 sobre una partición disjunta del entrenamiento es lo que da derecho al número."""
    f = [b for b in of(FIX, "facts")
         if any(r.get("label", "").startswith("cobertura") for r in b.get("rows", []))]
    assert f, "la tabla salió sin su calibración"
    etiquetas = {r["label"] for r in f[0]["rows"]}
    assert "cobertura objetivo" in etiquetas and "cobertura empírica medida" in etiquetas


def test_la_molecula_se_dibuja_y_no_solo_se_nombra():
    assert of(FIX, "molecule2d")[0]["smiles"] == FIX["results"][0]["smiles"]


def test_el_subarbol_del_admet_no_se_vuelca_dos_veces():
    """Sin marcar lo consumido, el recorrido genérico repetía endpoint por endpoint debajo de la tabla: 27
    bloques duplicados, medidos."""
    bs = blocks(FIX)
    assert len(bs) < 18, f"{len(bs)} bloques para seis endpoints: el árbol se está volcando dos veces"


# ------------------------------------------------------------------ el sobre: sólo lo que faltaba
ENV = {"visualization": {
    "card": {"title": "Antibody design", "modality": "antibody", "verdict": "REVIEW",
             "verdict_note": "developability bajo el umbral",
             "metrics": {"iptm": 0.71, "plddt": None},
             "comparison_axes": [{"axis": "afinidad", "peptide": 1.2, "antibody": 0.4, "aligned": True},
                                 {"axis": "estabilidad", "peptide": 3, "antibody": 9, "aligned": False}]},
    "molecular": {"available": True, "format": "pdb", "title": "Antibody (VH/VL) : PD-L1 complex",
                  "note": "Predicted complex — not experimental.",
                  "chain_labels": {"A": "VH (heavy)", "B": "VL (light)", "C": "Antigen (PD-L1)"}},
    "downloads": {"files": {"report.pdf": "/api/reports/download/x/report.pdf"}}}}


def test_el_veredicto_es_un_veredicto_y_no_una_fila():
    b = [x for x in of(ENV, "status", "antibody_discovery") if x["word"] == "REVIEW"]
    assert b and b[0]["where"] == "developability bajo el umbral"


def test_un_eje_sin_emparejar_se_marca_en_su_columna():
    """Una ventaja entera de este proyecto resultó ser el valor por defecto de disparos de un brazo. Un eje que
    BioQL marca `aligned: false` no es una comparación."""
    t = [b for b in of(ENV, "table", "antibody_discovery") if "emparejado" in (b.get("columns") or [])][0]
    assert [r for r in t["rows"] if r[0] == "estabilidad"][0][4] == "NO EMPAREJADO"
    assert [r for r in t["rows"] if r[0] == "afinidad"][0][4] == "sí"
    aus = " ".join(b["why"] for b in of(ENV, "absent", "antibody_discovery"))
    assert "no corrieron con la misma configuración" in aus


def test_una_metrica_nombrada_y_no_medida_se_dice():
    aus = " ".join(b["why"] for b in of(ENV, "absent", "antibody_discovery"))
    assert "plddt" in aus and "no las midió" in aus


def test_que_cadena_es_que_sale_en_una_tabla():
    """Sin esto un complejo anticuerpo-antígeno se ve como un borrón único; lo dice el propio comentario de
    BioQL al construir el campo."""
    t = [b for b in of(ENV, "table", "antibody_discovery") if b.get("columns") == ["cadena", "qué es"]]
    assert t and ["C", "Antigen (PD-L1)"] in t[0]["rows"]


def test_una_estructura_no_disponible_trae_su_motivo():
    p = {"visualization": {"molecular": {"available": False,
                                         "reason": "server-side render exceeded 600s — deferred"}}}
    aus = of(p, "absent", "peptide_discovery")
    assert aus and "exceeded 600s" in aus[0]["why"]


@pytest.mark.parametrize("mala", ["//otro/host.png", "javascript:alert(1)", "data:text/html,x"])
def test_una_descarga_a_otro_origen_no_se_arrastra(mala):
    """La misma regla que `cards.py`, y no una propia: un enlace es un ancla, y `//host` es otro origen."""
    p = {"visualization": {"downloads": {"files": {"x": mala}}}}
    for b in of(p, "links", "peptide_discovery"):
        assert all(i["url"] != mala for i in b["items"]), f"se arrastró {mala}"


def test_una_ruta_del_mismo_origen_si_se_enlaza():
    b = of(ENV, "links", "antibody_discovery")
    assert b and any(i["url"] == "/api/reports/download/x/report.pdf" for i in b[0]["items"])
