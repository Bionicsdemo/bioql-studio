"""Donde BioQL es flojo, se completa — y no se marca quién.

El encargo es que la app dé un resultado que ninguna de las dos partes da sola, y que se lea como UNO. El dueño
lo pidió así el 2026-11-21: «no quiero etiquetas de que hizo Claude, no hace falta que se muestren los resultados
sin importar quién lo hizo». Así que de la pantalla se fue la marca de autor — y NO se fue la fuente.

Esa distinción es la que sostiene todo lo demás. Este proyecto envió una vez **tres dosis distintas — 51,2, 49,3
y 48,8 mg — para la misma entrada** porque un valor plausible se puso donde se leía una medida (CLAUDE.md,
trampas de 2026-09-18). Lo que separa esos dos casos no es una etiqueta que diga quién puso el número: es que el
número diga DE DÓNDE SALE. `basis` sigue siendo obligatorio y sin él se refusa; lo que ya no existe es el cartel.

Y los huecos de BioQL siguen sin ser todos iguales, porque la diferencia la dice el propio BioQL en su razón:

- **le falta una ENTRADA** (`need receptor sequence to fold the complex`, `No module named ...`): BioQL sabe
  calcularlo. Eso no se rellena, se CIERRA — se consigue el dato y se vuelve a llamar, y el número que sale es
  suyo y medido. Es lo más valioso que Claude puede hacer aquí.
- **no tiene MODELO** (`no validated per-molecule dose model`): BioQL se niega a propósito. Ahí cabe
  conocimiento externo, con su fuente, dentro de la misma respuesta.

El bloque sigue siendo un tipo PROPIO en los datos, y eso tampoco es decoración: así no puede caer en `facts` ni
entrar en una puntuación. Esa separación es estructural y no se ve — que es justo lo que se pidió.
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


def test_sin_alcance_declarado_la_app_no_escribe_una_frase(studio_home):
    """El defecto anterior: a falta de `not_a` la app ponía «no es una medida de BioQL», que es a la vez una
    frase que nadie escribió y una marca de autor. No hay defecto: si no se dice el alcance, no se dibuja nada."""
    args = {k: v for k, v in BUENO.items() if k != "not_a"}
    out = claude_tools.handle("studio_contribute", args, None)
    b = [x for x in out["card"]["blocks"] if x["type"] == "contributed"][0]
    assert "not_a" not in b


def test_el_hueco_que_contesta_va_al_registro_y_no_encima_de_su_respuesta(studio_home):
    """Repetir «BioQL no predice esto» justo encima del valor que lo contesta es contradecirse en dos líneas. El
    hueco ya sale en la tarjeta del resultado; aquí vive en el registro, que es donde sirve para auditar."""
    out = claude_tools.handle("studio_contribute", dict(BUENO), None)
    assert not [x for x in out["card"]["blocks"] if x["type"] == "absent"]
    filas = [json.loads(l) for l in ledger.path_file().read_text().splitlines() if l.strip()]
    mio = [f for f in filas if f.get("tool") == "studio_contribute"][-1]
    assert "no validated per-molecule model" in mio["contribution"]["about_gap"]


def test_la_tarjeta_se_llama_por_lo_que_es(studio_home):
    out = claude_tools.handle("studio_contribute", dict(BUENO), None)
    assert out["card"]["title"] == BUENO["what"]
    assert "Claude" not in json.dumps(out["card"], ensure_ascii=False)


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


def test_la_ventana_no_dice_de_quien_es_pero_si_de_donde_sale():
    """EL test del encargo nuevo. Integrado significa que no se pueda señalar: las mismas clases que una fila de
    datos cualquiera, y debajo la fuente."""
    assert 'b.type === "contributed"' in JS
    i = JS.index('b.type === "contributed"')
    cuerpo = JS[i:i + 1600]
    assert '"crow-k"' in cuerpo and '"crow-v"' in cuerpo, "no se dibuja como una fila"
    assert "fuente: " in cuerpo, "no se dibuja de dónde sale"


def test_la_fila_no_lleva_borde_ni_cartel_propio():
    """Un recuadro que sólo le toca a estos valores sigue siendo una marca, aunque no diga nada."""
    css = (RAIZ / "app" / "web" / "app.css").read_text()
    i = css.index(".crow {")
    bloque = css[i:i + 500]
    assert "border" not in bloque and "dashed" not in bloque
    # y las medidas son las de `.fact`, no unas propias
    assert "font-size: 10.5px" in bloque and "font-size: 14px" in bloque


def test_un_hueco_cerrable_se_dibuja_distinto_del_que_no():
    assert "gap-" in JS and "gap-fix" in JS
    assert ".absent-block.gap-entrada" in (RAIZ / "app" / "web" / "app.css").read_text()


# ------------------------------------------------------------------ y el cerebro sabe la diferencia
def test_el_prompt_distingue_cerrar_un_hueco_de_rellenarlo():
    from studio import brain
    plano = " ".join(brain.SYSTEM.split())
    assert "missing an INPUT is not a dead end: close it" in plano
    assert "CALL BIOQL AGAIN" in plano
    assert "no MODEL behind it is where the rest of what you know belongs" in plano
    assert "Do not announce it, do not frame it as yours" in plano


def test_el_prompt_nombra_el_incidente_que_justifica_la_regla():
    """Una regla sin su motivo se relaja a la primera. El motivo son tres dosis reales."""
    from studio import brain
    plano = " ".join(brain.SYSTEM.split())
    assert "51.2, 49.3 and 48.8 mg" in plano
    assert "Never overwrite a number BioQL computed" in plano
    assert "a citation is what tells those apart" in plano
