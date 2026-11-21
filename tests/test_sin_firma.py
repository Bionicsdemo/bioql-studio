"""Ningún resultado de esta app lleva firma de quién lo puso.

El dueño lo pidió el 2026-11-21, con estas palabras: «si mi intención es la mayor integración posible y no quiero
etiquetas de que hizo Claude, sácala; no hace falta que se muestren los resultados sin importar quién lo hizo, no
lo marques». Antes de eso la app dibujaba un recuadro de línea discontinua con el cartel **aporta Claude** encima
de cualquier valor que no fuera de BioQL, y la tarjeta se llamaba **Aporte de Claude**.

Quitar las cuatro cadenas no es el trabajo: el trabajo es que no vuelvan. Y vuelven solas, porque la app lleva
meses escrita alrededor de la idea contraria — que la costura se viera — y el prompt del cerebro, las
descripciones MCP y los renderizadores repiten esa idea en sitios distintos. El mismo defecto ya pasó una vez en
este proyecto en la dirección opuesta: se quitó del código la puerta de aprobación y la frase se quedó viva en 335
descripciones (`test_nada_promete_una_puerta.py`). Un barrido que hay que acordarse de hacer no es un guardia.

**Lo que este fichero NO dice.** No dice que la procedencia se vaya. `basis` sigue siendo obligatorio y
`studio_contribute` sigue refusando sin él; el registro firmado sigue guardando `by: "claude"` en cada línea. Esa
es la diferencia entera: **de dónde sale** un número es información que se muestra, y **quién lo puso** es
contabilidad que se guarda y no se dibuja. Un valor sin fuente sigue sin entrar.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent

# Marcas de AUTOR. No entra aquí nada que hable de la FUENTE de un número: eso se muestra a propósito.
FIRMAS = re.compile(
    r"aport\w*\s+(?:de\s+|por\s+)?Claude"                 # "aporta Claude", "Aporte de Claude"
    r"|\baportaste\b"                                      # "lo que aportaste tú"
    r"|Claude\s+(?:lo\s+)?(?:aport|a[ñn]ad|puso|complet|calcul)"
    r"|(?:aportado|a[ñn]adido|puesto|rellenado|completado|estimado)\s+por\s+(?:Claude|el modelo|la IA)"
    r"|no es una medida de BioQL"
    r"|by Claude|from Claude|Claude's (?:value|number|estimate|contribution|guess)",
    re.I)

# Sitios donde una marca de autor sí corresponde, cada uno con su motivo:
PERMITIDO = (
    '"by": "claude"',          # el registro firmado: contabilidad, no pantalla
    'by="claude"',             # idem, en las llamadas del adaptador
)


def _vivo(py: Path) -> str:
    """El fichero sin docstrings. Los comentarios caen solos al pasar por el AST — y hace falta que caigan: estos
    ficheros CITAN las frases viejas para explicar por qué se fueron (CLAUDE.md, 2026-09-18)."""
    tree = ast.parse(py.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            b = node.body
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) \
                    and isinstance(b[0].value.value, str):
                node.body = b[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


def hallazgos() -> list[str]:
    out = []
    for py in sorted(RAIZ.glob("studio/*.py")) + sorted(RAIZ.glob("app/*.py")):
        for m in FIRMAS.finditer(_vivo(py)):
            trozo = m.string[max(0, m.start() - 70):m.end() + 70]
            if not any(ok in trozo for ok in PERMITIDO):
                out.append(f"{py.name}: …{trozo.strip()[:140]}…")
    for nombre in ("app.js", "app.css"):
        for i, l in enumerate((RAIZ / "app" / "web" / nombre).read_text().splitlines(), 1):
            t = l.strip()
            if t.startswith("//") or t.startswith("*") or t.startswith("/*"):
                continue
            m = FIRMAS.search(l)
            if m and not any(ok in l for ok in PERMITIDO):
                out.append(f"{nombre}:{i}: {t[:140]}")
    return out


def test_nada_vivo_firma_un_resultado():
    malos = hallazgos()
    assert not malos, "texto vivo que marca de quién es un valor:\n  " + "\n  ".join(malos)


@pytest.mark.parametrize("frase", [
    'TAG = "aporta Claude"',                                  # el cartel que había
    'TITULO = "Aporte de Claude"',                             # el título de la tarjeta
    'NOTA = "no es una medida de BioQL"',                      # el defecto de `not_a`
    'PIE = "lo que midió BioQL y lo que aportaste tú"',        # la nota que volvía a Claude
    'X = "este valor fue estimado por el modelo"',             # la variante sin nombrarlo
])
def test_el_guardia_salta_con_las_frases_reales(frase):
    """Un guardia que nunca falla es el mismo defecto que previene. Las cinco frases son las que de verdad
    estaban en el programa esta mañana, una por una."""
    objetivo = RAIZ / "studio" / "workflows.py"
    original = objetivo.read_text()
    try:
        objetivo.write_text(original + f"\n{frase}\n")
        assert hallazgos(), f"el guardia no vio {frase!r}"
    finally:
        objetivo.write_text(original)


def test_la_fuente_no_es_una_firma_y_sigue_siendo_obligatoria(studio_home):
    """La línea que este fichero defiende. Se fue QUIÉN; no se fue DE DÓNDE."""
    from studio import claude_tools
    sin = claude_tools.handle("studio_contribute", {"what": "x", "value": "1 mg"}, None)
    assert sin["state"] == "REFUSED"
    con = claude_tools.handle("studio_contribute",
                              {"what": "x", "value": "1 mg", "basis": "Hay et al. 2014"}, None)
    b = [x for x in con["card"]["blocks"] if x["type"] == "contributed"][0]
    assert b["basis"] == "Hay et al. 2014"
    assert not FIRMAS.search(str(con["card"]))


def test_el_registro_sigue_sabiendo_de_quien_fue(studio_home):
    """Y la contabilidad sigue entera: la pantalla no lo dice, el registro firmado sí. Sin esto, lo que se pidió
    sería perder la capacidad de separar — y eso es lo que costó las tres dosis."""
    import json

    from studio import claude_tools, ledger
    claude_tools.handle("studio_contribute",
                        {"what": "x", "value": "1 mg", "basis": "Hay et al. 2014"}, None)
    filas = [json.loads(l) for l in ledger.path_file().read_text().splitlines() if l.strip()]
    mio = [f for f in filas if f.get("tool") == "studio_contribute"][-1]
    assert mio["by"] == "claude" and mio["contribution"]["basis"] == "Hay et al. 2014"
