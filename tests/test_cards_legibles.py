"""Que una tarjeta de un pipeline grande se pueda leer.

La corrida PD-L1 real (`47507341-379`) producía **268 bloques**, 147 de ellos ausencias, casi todas "null in the
payload, with no reason given", y 36 tablas de las que la mayoría eran la misma repetida. Eso no es una tarjeta:
es el resultado volcado. Y entierra las cuatro frases que un científico necesita leer — que el término de unión
no entró en la puntuación, que el generador entrenado no se cargó, que dos validaciones cruzadas no corrieron.

Tres reglas, y cada una con su límite para no pasarse de lista:

1. **Un subárbol repetido se dibuja una vez.** BioQL devuelve el ensayo clínico bajo cinco nombres idénticos.
2. **Una razón repetida es un hecho, no treinta.** "clinical probability-of-success not predicted here" salía
   36 veces.
3. **Pero en un resultado pequeño cada hueco importa por su nombre.** Hasta doce, uno por uno: quien lee un
   ADMET quiere saber que `ld50_zhu` salió nulo.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from studio import cards

REAL = Path("/Users/heinzjungbluth/.bioql/artifacts/47507341-379/result.json")


def blocks(p, tool=None):
    return cards.card(p, tool=tool)["blocks"]


def of(p, kind, tool=None):
    return [b for b in blocks(p, tool) if b.get("type") == kind]


# ------------------------------------------------------------------ 1. copias
def test_una_rama_identica_a_otra_se_dibuja_una_vez():
    p = {"clinical_trial": {"phase_1": {"mtd": 10, "dose": 5, "n": 30}},
         "clinical_trials": {"phase_1": {"mtd": 10, "dose": 5, "n": 30}},
         "best_candidate": {"clinical_trial": {"phase_1": {"mtd": 10, "dose": 5, "n": 30}}}}
    bs = blocks(p)
    copia = [b for b in bs if b.get("type") == "text" and "copia literal" in b.get("text", "")]
    assert copia, "no se dijo que hay ramas repetidas"
    assert "rama(s)" in copia[0]["text"]
    # y los números salen una sola vez
    filas = [r["label"] for b in bs if b["type"] == "facts" for r in b["rows"]]
    assert sum(1 for f in filas if f.endswith(".mtd")) == 1, filas


def test_dos_ramas_distintas_no_se_confunden():
    p = {"a": {"x": 1, "y": 2, "z": 3}, "b": {"x": 1, "y": 2, "z": 4}}
    filas = [r["label"] for b in blocks(p) if b["type"] == "facts" for r in b["rows"]]
    assert "a.z" in filas and "b.z" in filas


def test_un_objeto_pequeno_no_se_pliega():
    """Dos claves iguales en dos sitios son una coincidencia, no una copia: plegarlas escondería datos."""
    p = {"a": {"n": 1}, "b": {"n": 1}}
    filas = [r["label"] for b in blocks(p) if b["type"] == "facts" for r in b["rows"]]
    assert "a.n" in filas and "b.n" in filas


# ------------------------------------------------------------------ 2. razones
def test_una_razon_repetida_se_dice_una_vez_con_su_cuenta():
    razon = "clinical probability-of-success not predicted here (no validated per-molecule model)"
    p = {f"fase_{i}": {"value": None, "null_reason": razon} for i in range(1, 31)}
    aus = of(p, "absent")
    mismos = [b for b in aus if b["why"] == razon]
    assert len(mismos) == 1, f"la misma razón salió {len(mismos)} veces"
    assert "30 campo(s)" in mismos[0]["what"]


def test_una_razon_unica_conserva_el_nombre_de_su_campo():
    p = {"clearance_hepatocyte_az": {"value": None, "null_reason": "no hepatocyte model for this scaffold"},
         "otro": {"value": None, "null_reason": "otra cosa"}}
    whys = {b["what"]: b["why"] for b in of(p, "absent")}
    assert whys["clearance_hepatocyte_az"] == "no hepatocyte model for this scaffold"


# ------------------------------------------------------------------ 3. el umbral
def test_pocos_huecos_salen_uno_a_uno_con_su_nombre():
    p = {f"endpoint_{i}": None for i in range(6)}
    nombres = {b["what"] for b in of(p, "absent")}
    assert "endpoint_3" in nombres, nombres


def test_muchos_huecos_se_agrupan_por_rama():
    p = {"rama": {f"campo_{i}": None for i in range(40)}}
    aus = of(p, "absent")
    assert len(aus) <= 4, f"{len(aus)} bloques para cuarenta nulos del mismo sitio"
    assert any("40 campo(s)" in b["what"] for b in aus)
    assert any("campo_0" in b["why"] for b in aus), "los nombres se perdieron del todo"


def test_justo_en_el_umbral_siguen_uno_a_uno():
    p = {f"e{i}": None for i in range(cards._Draw.BARE_ONE_BY_ONE)}
    assert len(of(p, "absent")) == cards._Draw.BARE_ONE_BY_ONE


# ------------------------------------------------------------------ `available: false`
def test_un_hueco_declarado_sale_como_ausencia_y_no_como_dos_filas():
    """Antes salía `available: False` y `reason: ...` como dos filas entre cientos."""
    p = {"hybrid_scoring": {"binding": {"available": False,
                                        "reason": "no quantum or AI binding component present this run"}}}
    aus = of(p, "absent", "peptide_discovery")
    assert aus and aus[0]["what"] == "hybrid_scoring.binding"
    assert aus[0]["why"] == "no quantum or AI binding component present this run"
    filas = [r["label"] for b in blocks(p, "peptide_discovery") if b["type"] == "facts" for r in b["rows"]]
    assert not any(f.endswith(".available") for f in filas), "el booleano volvió a salir como dato"


def test_un_hueco_declarado_sin_razon_lo_dice():
    p = {"x": {"available": False, "n": 1, "m": 2}}
    assert "no dice por qué" in of(p, "absent", "x")[0]["why"]


def test_available_true_no_genera_ruido():
    p = {"x": {"available": True, "valor": 7}}
    assert not [b for b in of(p, "absent") if b["what"].startswith("x")]
    filas = [r["label"] for b in blocks(p) if b["type"] == "facts" for r in b["rows"]]
    assert "x.valor" in filas and "x.available" not in filas


# ------------------------------------------------------------------ la galería
def test_la_galeria_solo_existe_si_hay_algo_que_mirar():
    """Doscientas baldosas sin estructura se leen peor que una tabla — que además sabe recortar y decir cuántas
    filas dejó fuera."""
    numeros = {"candidates": [{"id": i, "score": i / 100} for i in range(30)]}
    assert not of(numeros, "gallery")
    assert of(numeros, "table")
    conestructura = {"candidates": [{"id": 1, "smiles": "CCO", "score": 0.5}]}
    assert of(conestructura, "gallery")


def test_un_hueco_honesto_viaja_pegado_a_su_molecula():
    p = {"candidates": [{"name": "A", "smiles": "CCO", "composite_score": 0.75,
                         "binding_affinity_kcal_honest": {
                             "value": None,
                             "null_reason": "need receptor sequence to fold the complex"}}]}
    it = of(p, "gallery")[0]["items"][0]
    assert it["rows"], "los números se perdieron"
    assert any("need receptor sequence" in g for g in it.get("gaps", [])), it


def test_no_declarar_banda_de_sintesis_no_es_no_sintetizable():
    """La tarjeta de BioQL mete un candidato sin banda en el cubo 'unknown' y lo deja fuera de `makeable`, así
    que un resultado sin datos de síntesis enseña un '0/N sintetizables' en rojo. Eso es una ausencia disfrazada
    de veredicto."""
    p = {"candidates": [{"name": "A", "smiles": "CCO"}]}
    aus = " ".join(b["why"] for b in of(p, "absent"))
    assert "no declaran banda" in aus and "disfrazada de veredicto" in aus


# ------------------------------------------------------------------ sobre la corrida de verdad
@pytest.mark.skipif(not REAL.exists(), reason="no está el resultado PD-L1 en esta máquina")
def test_la_corrida_real_cabe_en_una_tarjeta():
    d = json.loads(REAL.read_text())
    bs = blocks(d, "peptide_discovery")
    assert len(bs) < 130, f"{len(bs)} bloques: la tarjeta volvió a ser un volcado (eran 268)"
    aus = [b for b in bs if b["type"] == "absent"]
    assert len(aus) < 45, f"{len(aus)} ausencias (eran 147)"
    # y las cuatro frases que importan siguen estando, enteras
    todo = " ".join(b.get("why", "") for b in aus)
    for frase in ("no quantum or AI binding component present this run",
                  "promoted checkpoint not found",
                  "geneformer_validator",
                  "need receptor sequence to fold the peptide:receptor complex"):
        assert frase in todo, f"se perdió: {frase}"


@pytest.mark.skipif(not REAL.exists(), reason="no está el resultado PD-L1 en esta máquina")
def test_la_molecula_de_la_corrida_real_se_dibuja_con_el_svg_de_bioql():
    d = json.loads(REAL.read_text())
    g = of(d, "gallery", "peptide_discovery")
    assert g, "el candidato no llegó a la galería"
    it = g[0]["items"][0]
    assert it["label"] == "PDL1-001"
    assert it["svg"].lstrip().startswith("<?xml") or it["svg"].lstrip().startswith("<svg")
    etiquetas = {r["label"] for r in it["rows"]}
    assert {"puntuación", "clase", "peso"} <= etiquetas, etiquetas


# ------------------------------------------------------------------ el visor rápido
def test_el_bolsillo_se_calcula_a_mano_y_no_con_el_selector_roto():
    """3Dmol trae un selector `within` para "los residuos a 8 Å del ligando" y en la versión que viaja dentro de
    la app devuelve CERO átomos — medido contra un complejo donde hay 45, en sus tres formas. Un botón que deja
    el lienzo en blanco es peor que no tener el botón, así que la distancia se calcula aquí.
    """
    js = (Path(__file__).resolve().parent.parent / "app" / "web" / "app.js").read_text()
    i = js.index('poner("Bolsillo"')
    cuerpo = js[i:i + 2000]
    assert "within:" not in cuerpo, "volvió el selector que devuelve cero"
    assert "selectedAtoms" in cuerpo and "dx * dx" in cuerpo, "ya no se calcula la distancia"
    # y cuando no hay bolsillo se dice, en vez de enseñar una vista vacía
    assert "no es un complejo acoplado" in cuerpo


def test_los_tres_modos_del_visor_existen():
    js = (Path(__file__).resolve().parent.parent / "app" / "web" / "app.js").read_text()
    for modo in ('poner("Cartoon"', 'poner("Superficie"', 'poner("Bolsillo"'):
        assert modo in js, f"falta el modo {modo}"
    # el de superficie necesita proteína; el de bolsillo, proteína Y ligando
    assert "if (prot >= 0 && lig >= 0 && parts.length > 1)" in js
