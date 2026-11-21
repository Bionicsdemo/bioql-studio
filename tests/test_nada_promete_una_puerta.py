"""Nada en esta app promete una puerta que ya no existe.

El 2026-11-20 se quitó del código la puerta de aprobación y la frase que la anunciaba se quedó en la descripción
de 335 herramientas MCP. Eso ya tiene su propio guardia (`test_descriptions_do_not_refuse.py`). Lo que este
fichero cubre es que el mismo defecto apareció **tres veces más esa misma noche, en sitios distintos**, y cada
una la encontró un barrido manual y no un test:

1. el pie de la ventana decía `502 tools · 335 ask first`;
2. la cabecera del chat y la tabla de uso contaban `0 approved` — un contador que no se incrementaba en ninguna
   parte del programa;
3. Ajustes decía `tools that ask a person`, y el catálogo de workflows clasificaba todo lo caro como
   `needs a person`, con un campo `needs_person` y un `HW_PERSON = "a person decides..."`.

Un barrido que hay que acordarse de hacer no es un guardia. Éste lo hace solo.

**Lo que NO cuenta como defecto**, y por qué cada excepción está nombrada aquí en vez de relajar la regla:

- `confirm()` de una campaña: esa puerta existe y es de una persona.
- la firma de releases: refusa de verdad, con una clave.
- el runner (Claude Code) pidiendo permiso: esa puerta es suya, no de Studio, y el texto lo dice.
- el cerebro ausente: "Chat cannot run without it" es cierto.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent

# Frases que prometen que ALGO DE STUDIO va a parar una llamada por falta de permiso.
PROMESAS = re.compile(
    r"needs? (?:a |an )?(?:person|human|owner)'?s?(?! own)"
    r"|asks? (?:a |the )?person"
    r"|ask first"
    r"|tools that ask"
    r"|cannot run it"
    r"|must approve"
    r"|requires? approval"
    r"|pide aprobaci|pregunta a una persona",
    re.I)

# Cada excepción, nombrada. Si alguien añade una, tiene que escribir por qué aquí.
PERMITIDO = (
    "a confirmation needs a person",          # campaign.confirm(): esa puerta sí existe
    "refuse releases signed with this key",   # la firma del canal de actualizaciones
    "Claude Code (el runner)",                # la puerta del runner, declarada como suya
    "Chat cannot run without it",             # el cerebro ausente: es verdad
)


def _vivo(py: Path) -> str:
    """El fichero sin docstrings. Los comentarios no entran en el AST, así que caen solos — y hacen falta que
    caigan: estos ficheros CITAN las frases viejas para explicar por qué se fueron (CLAUDE.md, 2026-09-18: un
    guardia no debe leer su propia explicación)."""
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
        for m in PROMESAS.finditer(_vivo(py)):
            trozo = m.string[max(0, m.start() - 80):m.end() + 80]
            if not any(ok in trozo for ok in PERMITIDO):
                out.append(f"{py.name}: …{trozo.strip()[:150]}…")
    js = (RAIZ / "app" / "web" / "app.js").read_text().splitlines()
    for i, l in enumerate(js, 1):
        if l.strip().startswith("//") or l.strip().startswith("*"):
            continue
        m = PROMESAS.search(l)
        if m and not any(ok in l for ok in PERMITIDO):
            out.append(f"app.js:{i}: {l.strip()[:150]}")
    return out


def test_nada_vivo_promete_una_puerta_que_no_existe():
    malos = hallazgos()
    assert not malos, "texto vivo que promete una puerta retirada:\n  " + "\n  ".join(malos)


def test_el_guardia_salta_cuando_debe(tmp_path, monkeypatch):
    """Un guardia que nunca falla es el mismo defecto que previene. Se le inyectan las cuatro frases reales que
    se encontraron esa noche, una por una."""
    objetivo = RAIZ / "studio" / "workflows.py"
    original = objetivo.read_text()
    for frase in ('PENDIENTE = "needs a person"',
                  'ETIQUETA = "tools that ask a person"',
                  'AVISO = "a person must approve it"',
                  'NOTA = "requires approval before running"'):
        try:
            objetivo.write_text(original + f"\n{frase}\n")
            assert hallazgos(), f"el guardia no vio {frase!r}"
        finally:
            objetivo.write_text(original)


def test_el_guardia_no_se_lee_a_si_mismo():
    """Este fichero CITA las frases prohibidas para explicarlas. Si el barrido mirara los tests, fallaría por su
    propia documentación — que es exactamente el defecto que este proyecto ya envió dos veces."""
    assert "tests" not in str(RAIZ / "studio"), "sanity"
    # el barrido sólo mira studio/ y app/, nunca tests/
    assert not any(h.startswith("test_") for h in hallazgos())


@pytest.mark.parametrize("permitida", PERMITIDO)
def test_cada_excepcion_sigue_existiendo(permitida):
    """Una excepción que ya no corresponde a nada es un agujero abierto por si acaso. Si el código que la
    justificaba desapareció, esto lo dice."""
    textos = []
    for py in list(RAIZ.glob("studio/*.py")) + list(RAIZ.glob("app/*.py")):
        textos.append(py.read_text())
    textos.append((RAIZ / "app" / "web" / "app.js").read_text())
    assert any(permitida in t for t in textos), f"la excepción {permitida!r} ya no protege nada: quítala"
