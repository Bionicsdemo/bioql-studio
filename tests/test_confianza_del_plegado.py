"""Cuánto se puede creer el plegado que la tarjeta acaba de dibujar.

Una proteína dibujada sin su pLDDT es una imagen que miente por omisión. Con pLDDT 75 — el valor real de la
corrida `ps-73cb98fc75e9` — el esqueleto está bien y los bucles y las cadenas laterales no, así que un acople
metido en un bucle de esa estructura no quiere decir nada. Y con ipTM 0,566 — el valor real de la corrida PD-L1
`9a261d5c-b6f` — la interfaz que se está mirando probablemente no es la que existe.

BioQL trae los dos números y Studio los enterraba: un payload grande tiene 175 de estos escalares repartidos por
el árbol, y salían como una fila entre cientos. Ahora van al lado de la estructura, en su chip.

**Lo que estos tests defienden no es el chip, es la asociación.** Un número de confianza pegado a la estructura
equivocada es peor que no tenerlo, y hay tres maneras de equivocarse, las tres medidas sobre los payloads reales:

1. **la escala.** Un `plddt` de 0,75 puede ser 0,75 sobre 1 (bueno) o 0,75 sobre 100 (catastrófico), y el payload
   no lo dice. Entonces no hay palabra: va el número desnudo.
2. **el parentesco.** `9a261d5c` tiene un `iptm` de un complejo de proteína en un nodo, y si la estructura
   colgara de la raíz se lo llevaría por estar en el mismo payload. Sin nodo padre no hay hermandad.
3. **la clase de molécula.** pLDDT, pTM e ipTM son métricas de un PLEGADO. Un SDF no se pliega.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from studio import cards
from studio.cards import _conf_band, _confidence_of

ARTEFACTOS = Path.home() / ".bioql" / "artifacts"
PDB = ("ATOM      1  N   MET A   1      0.000   0.000   0.000  1.00 20.00           N\n"
       "ATOM      2  CA  MET A   1      1.458   0.000   0.000  1.00 20.00           C\nEND\n")


def bloque3d(spec):
    return next((b for b in spec["blocks"] if b["type"] == "molecule3d"), None)


# ------------------------------------------------------------------ las bandas son las publicadas
@pytest.mark.parametrize("kind,v,palabra,tono", [
    ("pLDDT", 95.0, "muy alta", "clean"),
    ("pLDDT", 74.99747276306152, "alta sólo en el esqueleto", "neutral"),   # el valor real de ps-73cb98fc75e9
    ("pLDDT", 62.0, "baja: bucles no fiables", "concerns"),
    ("pLDDT", 31.0, "no fiable", "bad"),
    ("ipTM", 0.91, "interfaz fiable", "clean"),
    ("ipTM", 0.5658, "interfaz probablemente mal", "bad"),                  # el valor real de 9a261d5c-b6f
    ("pTM", 0.6513, "plegado global correcto", "clean"),
])
def test_cada_banda_dice_lo_que_dice_la_escala_publicada(kind, v, palabra, tono):
    assert _conf_band(kind, v) == (palabra, tono)


def test_entre_70_y_90_no_se_pinta_de_verde():
    """AlphaFold llama «confident» a esa banda, y confident significa esqueleto sí y cadenas laterales no. Un
    chip verde sobre un 75 invita justo al error que este proyecto persigue."""
    _, tono = _conf_band("pLDDT", 75.0)
    assert tono == "neutral"
    assert _conf_band("pLDDT", 91.0)[1] == "clean"


def test_una_escala_ambigua_no_recibe_palabra():
    """EL test de la escala. 0,75 en un campo llamado `plddt` no se interpreta: ni «no fiable» ni «muy alta»."""
    assert _conf_band("pLDDT", 0.75) is None
    assert _conf_band("ipTM", 57.0) is None          # un ipTM que no está en 0-1 tampoco se bandea


# ------------------------------------------------------------------ la asociación
def test_sin_nodo_padre_no_hay_hermandad():
    """EL test del parentesco. Una estructura en la raíz no tiene hermanas: lo que haya en el primer nivel está
    en el mismo payload, no es sobre ella."""
    assert _confidence_of({"plddt": 88.0, "mol": PDB}, "", set()) == []


def test_una_estructura_con_su_nodo_se_lleva_la_confianza_de_ese_nodo():
    p = {"a": {"pdb": PDB, "plddt": 88.0}, "b": {"pdb": PDB, "plddt": 41.0}}
    assert _confidence_of(p, "a", set())[0]["value"] == 88.0
    assert _confidence_of(p, "b", set())[0]["value"] == 41.0


def test_tambien_dentro_de_un_confidence_porque_asi_lo_escribe_protenix():
    """Medido: `candidates[0].analyses.protenix_structure.confidence.{plddt,ptm,iptm}`."""
    got = _confidence_of({"s": {"pdb": PDB, "confidence": {"plddt": 68.68, "iptm": 0.5658}}}, "s", set())
    assert {g["kind"] for g in got} == {"pLDDT", "ipTM"}


def test_lo_que_se_ensena_se_marca_consumido_y_no_vuelve_como_fila_suelta():
    consumido: set[str] = set()
    _confidence_of({"s": {"pdb": PDB, "plddt": 88.0}}, "s", consumido)
    assert "s.plddt" in consumido


def test_un_sdf_no_se_pliega_y_no_lleva_plddt():
    """EL test de la clase. Sólo se buscan estas métricas cuando lo dibujado incluye una macromolécula."""
    sdf = "\n  RDKit 3D\n\n  1  0  0  0  0  0  0  0  0  0999 V2000\n    0.0 0.0 0.0 C   0  0\nM  END\n"
    spec = cards.card({"paquete": {"ligand_sdf": sdf, "plddt": 88.0}})
    b = bloque3d(spec)
    assert b and "confidence" not in b


def test_una_proteina_si(studio_home):
    spec = cards.card({"paquete": {"protein_pdb": PDB, "plddt": 92.4}})
    b = bloque3d(spec)
    assert b and b["confidence"][0] == {"kind": "pLDDT", "value": 92.4, "where": "paquete.plddt",
                                        "word": "muy alta", "tone": "clean"}


# ------------------------------------------------------------------ y la ventana lo dibuja
def test_la_ventana_dibuja_el_chip_con_su_ruta():
    js = (Path(__file__).resolve().parent.parent / "app" / "web" / "app.js").read_text()
    i = js.index("(b.confidence || []).forEach")
    cuerpo = js[i:i + 900]
    assert "escala no declarada" in cuerpo, "un valor sin banda tiene que decir por qué no la tiene"
    assert "BioQL lo trae en" in cuerpo, "no se puede comprobar de dónde salió el número"
    assert "toFixed(1)" in cuerpo and "toFixed(3)" in cuerpo


# ------------------------------------------------------------------ sobre los payloads reales de esta máquina
@pytest.mark.parametrize("corrida,kind,valor", [
    ("ps-73cb98fc75e9", "pLDDT", 74.99747276306152),
    ("9a261d5c-b6f", "ipTM", 0.5658),
])
def test_sobre_las_corridas_reales_de_esta_maquina(corrida, kind, valor):
    f = ARTEFACTOS / corrida / "result.json"
    if not f.exists():
        pytest.skip(f"no hay {f}")
    b = bloque3d(cards.card(json.loads(f.read_text()), tool="pipeline"))
    assert b, "esa corrida traía una estructura y ya no se dibuja"
    got = {c["kind"]: c["value"] for c in b.get("confidence", [])}
    assert got.get(kind) == pytest.approx(valor), got
