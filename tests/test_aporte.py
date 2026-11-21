"""Donde BioQL es flojo, Claude completa — y la costura se tiene que ver.

El encargo es que la app dé un resultado que ninguna de las dos partes da sola. La línea está en un sitio que
este proyecto ya cruzó una vez: envió **tres dosis distintas — 51,2, 49,3 y 48,8 mg — para la misma entrada**
porque un valor plausible se puso donde se leía una medida (CLAUDE.md, trampas de 2026-09-18).

Así que los huecos de BioQL no son todos iguales, y la diferencia la dice el propio BioQL en su razón:

- **le falta una ENTRADA** (`need receptor sequence to fold the complex`, `No module named ...`): BioQL sabe
  calcularlo. Eso no se rellena, se CIERRA — se consigue el dato y se vuelve a llamar, y el número que sale es
  suyo y medido. Es lo más valioso que Claude puede hacer aquí.
- **no tiene MODELO** (`no validated per-molecule dose model`): BioQL se niega a propósito. Ahí cabe
  conocimiento externo, en su propio bloque, con su base y con lo que NO es.

`basis` es obligatorio y sin él se refusa. Esa es la única regla que separa un aporte de un número inventado.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from studio import cards_gaps, claude_tools, ledger
from studio.cards_gaps import classify

RAIZ = Path(__file__).resolve().parent.parent
JS = (RAIZ / "app" / "web" / "app.js").read_text()

BUENO = {"what": "probabilidad de éxito clínico",
         "value": "~10% de fase 1 a aprobación en oncología",
         "basis": "Hay et al. 2014, Nat Biotechnol 32(1):40-51 — tasa base por modalidad e indicación",
         "not_a": "no es una predicción para este péptido",
         "about_gap": "clinical probability-of-success not predicted here (no validated per-molecule model)"}


# ------------------------------------------------------------------ de qué clase es cada hueco
@pytest.mark.parametrize("why,clase,falta", [
    ("need receptor sequence to fold the peptide:receptor complex for MM-GBSA", "entrada", "receptor sequence"),
    ("ModuleNotFoundError: No module named 'geneformer_validator'", "entrada", "geneformer_validator"),
    ("promoted checkpoint not found: /checkpoints/bioql_esm2_hla_a0201_v1.pt", "entrada", "checkpoint not found"),
    ("dose not predicted here (no validated per-molecule dose model)", "modelo", None),
    ("clinical probability-of-success not predicted here (no validated per-molecule model)", "modelo", None),
    ("the payload holds an empty list", "otro", None),
])
def test_la_clase_del_hueco_sale_del_texto_de_bioql(why, clase, falta):
    c, f = classify(why)
    assert c == clase
    if falta is None:
        assert f is None
    else:
        assert f and falta in f


def test_lo_que_falta_se_cita_y_no_se_adivina():
    """Si BioQL no dice qué le falta, no se inventa: la clase puede ser 'modelo' y el qué, None."""
    _, f = classify("no validated per-molecule dose model; hardcoded default removed")
    assert f is None


def test_un_hueco_de_entrada_llega_al_bloque_con_lo_que_falta(studio_home):
    p = {"mmgbsa": {"available": False,
                    "reason": "need receptor sequence to fold the peptide:receptor complex for MM-GBSA"}}
    b = cards_gaps.gap_blocks(p, set())[0]
    assert b["gap"] == "entrada" and "receptor sequence" in b["fix"]


def test_un_hueco_sin_modelo_no_trae_un_falta_inventado(studio_home):
    p = {"dosis": {"available": False, "reason": "no validated per-molecule dose model"}}
    b = cards_gaps.gap_blocks(p, set())[0]
    assert b["gap"] == "modelo" and "fix" not in b


# ------------------------------------------------------------------ la regla que lo hace seguro
@pytest.mark.parametrize("falta", ["what", "value", "basis"])
def test_un_aporte_sin_su_base_se_refusa(falta, studio_home):
    """EL test. Un aporte sin de dónde sale es indistinguible de un número inventado."""
    args = {k: v for k, v in BUENO.items() if k != falta}
    out = claude_tools.handle("studio_contribute", args, None)
    assert out["state"] == "REFUSED"
    assert "basis" in out["detail"] and "inventado" in out["detail"]


def test_un_aporte_con_base_queda_en_el_registro_firmado(studio_home):
    out = claude_tools.handle("studio_contribute", dict(BUENO), None)
    assert out["state"] == "RESOLVED" and out["recorded"] is True
    filas = [json.loads(l) for l in ledger.path_file().read_text().splitlines() if l.strip()]
    mia = [f for f in filas if f.get("tool") == "studio_contribute"]
    assert mia, "el aporte no llegó al registro"
    c = mia[-1]["contribution"]
    assert c["basis"].startswith("Hay et al. 2014")
    assert c["not_a"] == "no es una predicción para este péptido"
    assert mia[-1]["by"] == "claude", "no se sabría de quién fue"


def test_sin_not_a_se_pone_uno_que_no_miente(studio_home):
    args = {k: v for k, v in BUENO.items() if k != "not_a"}
    out = claude_tools.handle("studio_contribute", args, None)
    b = [x for x in out["card"]["blocks"] if x["type"] == "contributed"][0]
    assert b["not_a"] == "no es una medida de BioQL"


def test_el_hueco_que_contesta_viaja_con_el(studio_home):
    out = claude_tools.handle("studio_contribute", dict(BUENO), None)
    aus = [x for x in out["card"]["blocks"] if x["type"] == "absent"]
    assert aus and "no validated per-molecule model" in aus[0]["why"]


# ------------------------------------------------------------------ no puede confundirse con una medida
def test_un_aporte_es_su_propio_tipo_de_bloque_y_no_una_fila():
    """Estructuralmente: `contributed` no es `facts`, así que no puede caer en la lista de datos de la tarjeta
    ni entrar en una puntuación. Esa separación es la que impide repetir las tres dosis."""
    from studio.cards import BLOCK_TYPES
    assert "contributed" in BLOCK_TYPES
    ALLOWED = {"what", "value", "basis", "not_a"}
    out = claude_tools.handle("studio_contribute", dict(BUENO), None)
    b = [x for x in out["card"]["blocks"] if x["type"] == "contributed"][0]
    assert set(b) - {"type"} <= ALLOWED
    assert "rows" not in b and "value" in b


def test_la_ventana_lo_marca_y_dice_que_no_es():
    assert 'b.type === "contributed"' in JS
    assert "aporta Claude" in JS
    assert "no es una medida de BioQL" in JS
    i = JS.index('b.type === "contributed"')
    cuerpo = JS[i:i + 1400]
    assert "contrib-b" in cuerpo and "sobre: " in cuerpo, "no se dibuja de dónde sale"


def test_un_hueco_cerrable_se_dibuja_distinto_del_que_no():
    assert "gap-" in JS and "gap-fix" in JS
    assert ".absent-block.gap-entrada" in (RAIZ / "app" / "web" / "app.css").read_text()


# ------------------------------------------------------------------ y el cerebro sabe la diferencia
def test_el_prompt_distingue_cerrar_un_hueco_de_rellenarlo():
    from studio import brain
    plano = " ".join(brain.SYSTEM.split())
    assert "missing an INPUT is not a dead end: close it" in plano
    assert "CALL BIOQL AGAIN" in plano
    assert "no MODEL behind it is where you contribute, labelled" in plano


def test_el_prompt_nombra_el_incidente_que_justifica_la_regla():
    """Una regla sin su motivo se relaja a la primera. El motivo son tres dosis reales."""
    from studio import brain
    plano = " ".join(brain.SYSTEM.split())
    assert "51.2, 49.3 and 48.8 mg" in plano
    assert "never let one read like a measurement" in plano
