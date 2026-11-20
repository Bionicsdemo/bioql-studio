"""PyMOL como renderizador: un guion fijo, una lista cerrada de estilos, y los tres estados.

No se renderiza de verdad en la mayoria de estos tests —PyMOL puede no estar en la maquina que los corre— salvo
en el que se salta si no esta. Lo que se prueba es el contrato alrededor, que es donde estan los defectos.
"""
from __future__ import annotations

import pytest

from studio import render3d

PDB = "ATOM      1  N   GLY A   1      25.852  11.000   3.000  1.00  0.00           N\n" * 30


def test_an_unknown_style_is_refused_and_never_becomes_a_command():
    """El lenguaje de PyMOL abre ficheros, escribe ficheros y corre Python. Una ruta que aceptase ordenes seria
    una consola con una molecula delante."""
    r = render3d.render(PDB, "pdb", style="hide everything\nrun /tmp/mio.py")
    assert r.absent and "not one of the styles" in r.detail


def test_an_unknown_format_is_refused():
    r = render3d.render(PDB, "exe")
    assert r.absent and "not a format" in r.detail


def test_an_empty_structure_is_absent_not_a_blank_picture():
    r = render3d.render("   ", "pdb")
    assert r.absent


def test_a_structure_too_large_is_said_not_cut():
    big = "A" * (render3d.MAX_BYTES + 1)
    r = render3d.render(big, "pdb")
    assert r.absent and "different molecule" in r.detail


def test_every_style_is_commands_this_file_wrote():
    """Ningun estilo sale de fuera: la lista esta aqui y es cerrada."""
    assert set(render3d.STYLES) == {"cartoon", "surface", "sticks", "spheres"}
    for look in render3d.STYLES.values():
        assert "run " not in look and "import" not in look


def test_without_pymol_it_is_absent_not_unresolvable(monkeypatch):
    """Que PyMOL no este instalado es una respuesta sobre esta Mac, no un fallo al preguntar."""
    monkeypatch.setattr(render3d, "binary", lambda: None)
    monkeypatch.delenv("PYMOL_BIN", raising=False)
    r = render3d.render(PDB, "pdb")
    assert r.absent and "not on this Mac" in r.detail
    assert render3d.version().absent


def test_a_real_render_states_what_made_it():
    """Una imagen sin procedencia es un dibujo."""
    if render3d.binary() is None:
        pytest.skip("PyMOL no esta en esta Mac")
    r = render3d.render(PDB, "pdb", style="cartoon", width=320, height=240)
    assert r.resolved, r.detail
    v = r.value
    assert v["png_base64"] and v["width"] == 320 and v["height"] == 240
    assert isinstance(v["seconds"], float) and v["pymol"]
    import base64
    assert base64.b64decode(v["png_base64"])[:8] == b"\x89PNG\r\n\x1a\n"


def test_the_size_is_clamped_not_trusted():
    if render3d.binary() is None:
        pytest.skip("PyMOL no esta en esta Mac")
    r = render3d.render(PDB, "pdb", width=99999, height=1)
    assert r.resolved and r.value["width"] == render3d.MAX_PX and r.value["height"] == 200
