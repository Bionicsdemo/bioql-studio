"""Los parámetros que BioQL lee, leídos de su código — y la línea que no se cruza.

De las 502 entradas del allowlist, **386 no declaraban un solo parámetro**: el registro de BioQL publica 63 y
nada más. Para las otras 386 Claude tenía la ruta y ninguna pista del contrato, así que adivinaba los nombres —
y una llamada con `target=` donde el handler lee `protein=` vuelve con un 400 que se lee como que BioQL está
roto. `tools/discover_params.py` los saca de los handlers: 1118 nombres para 254 entradas.

**La línea.** Un parámetro LEÍDO EN EL CÓDIGO no es un parámetro DECLARADO, y la diferencia decide a quién se le
rechaza una llamada:

- si entrara en `required`, una llamada legítima sin ese campo empezaría a fallar;
- si entrara en `optional`, `schema_known` pasaría a verdadero y el adaptador empezaría a refusar todo lo que no
  esté en una lista deducida — que es justo el defecto que ya costó 128 entradas listadas e inusables.

Así que vive en su propio campo y sólo alimenta la DESCRIPCIÓN. Estos tests son los que impiden que alguien, con
buena intención, lo "arregle" moviéndolo a `optional`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from studio import allowlist as al
from studio.adapter import Adapter, Refused
from studio.mcp_server import _describe

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))


def entrada(**extra):
    base = {"method": "POST", "path": "/api/x", "kind": "compute", "why": "una ruta"}
    base.update(extra)
    return al._norm("x", base)


# ------------------------------------------------------------------ el campo sobrevive y no contamina
def test_los_parametros_leidos_sobreviven_a_la_normalizacion():
    e = entrada(params_seen=["smiles", "width", "smiles"])
    assert e["params_seen"] == ["smiles", "width"], "se perdieron o se duplicaron"


def test_no_entran_en_required_ni_en_optional():
    e = entrada(params_seen=["smiles", "width"])
    assert e["required"] == [] and e["optional"] == []


def test_no_vuelven_verdadero_schema_known():
    """Si lo hicieran, el adaptador empezaría a refusar cualquier argumento fuera de una lista DEDUCIDA. Eso ya
    dejó 128 entradas listadas e inusables una vez."""
    e = entrada(params_seen=["smiles", "width"])
    assert e["schema_known"] is False


def test_una_entrada_sin_parametros_leidos_trae_una_lista_vacia_y_no_None():
    assert entrada()["params_seen"] == []


# ------------------------------------------------------------------ no cambian lo que se rechaza
def test_un_argumento_que_no_esta_en_la_lista_leida_sigue_pasando(fake_bioql):
    """EL test de la línea. La lista es una pista, no una puerta."""
    a = Adapter(fake_bioql, {"x": entrada(path="/api/smiles-to-3d", params_seen=["smiles"])})
    a.check("x", {"otro_campo": 1})          # no levanta


def test_un_argumento_reservado_se_sigue_refusando(fake_bioql):
    a = Adapter(fake_bioql, {"x": entrada(params_seen=["smiles"])})
    with pytest.raises(Refused):
        a.check("x", {"endpoint": "/api/scripts/execute"})


# ------------------------------------------------------------------ la descripción los dice, y dice de dónde
def test_la_descripcion_lista_los_nombres():
    """Ordenados: `_norm` los ordena a proposito, para que la misma entrada de siempre la misma descripcion."""
    d = _describe(entrada(params_seen=["pdb", "num_steps", "timeout_s"]))
    assert "lee: num_steps, pdb, timeout_s" in d


def test_la_descripcion_dice_que_salieron_del_codigo_y_no_del_registro():
    """El modelo merece saber la diferencia entre lo que BioQL declara y lo que alguien leyó en su fuente."""
    d = _describe(entrada(params_seen=["pdb"]))
    assert "Leidos de su codigo, no declarados en su registro" in d


def test_no_se_repite_lo_que_ya_estaba_declarado():
    d = _describe(entrada(required=["smiles"], optional=["width"], params_seen=["smiles", "width", "height"]))
    assert "lee: height" in d, d


def test_sin_parametros_leidos_la_descripcion_no_dice_nada_de_esto():
    assert "Su handler en BioQL lee" not in _describe(entrada())


# ------------------------------------------------------------------ el extractor
def test_el_extractor_sigue_la_variable_del_cuerpo_y_no_cualquier_get():
    """`os.environ.get("HOME")` y `result.get("x")` no son parámetros de la ruta. Si el extractor los tragara,
    las descripciones se llenarían de nombres que no existen — peor que no tener ninguno."""
    import discover_params as dp
    cuerpo = '''@app.route("/api/cosa", methods=["POST"])
def cosa():
    data = request.get_json() or {}
    smiles = data.get("smiles")
    n = data.get("n_steps", 10)
    modo = data["modo"]
    casa = os.environ.get("HOME")
    r = result.get("status")
    q = request.args.get("formato")
    return jsonify({})'''.splitlines()
    assert dp.params_of(cuerpo) == {"smiles", "n_steps", "modo", "formato"}


def test_el_extractor_aguanta_las_otras_formas_de_coger_el_cuerpo():
    import discover_params as dp
    for linea in ('    payload = request.json or {}', '    d = request.get_json(silent=True) or {}'):
        v = linea.strip().split()[0]
        assert dp.params_of([linea, f'    x = {v}.get("diana")']) == {"diana"}


def test_el_extractor_no_mira_mas_alla_del_siguiente_handler():
    import discover_params as dp
    lineas = ['@app.route("/api/a")', '    data = request.get_json()', '    x = data.get("mio")']
    cuerpo = dp._handler_body(lineas + ['@app.route("/api/b")', '    data = request.get_json()',
                                        '    y = data.get("ajeno")'], 0)
    assert dp.params_of(cuerpo) == {"mio"}


# ------------------------------------------------------------------ sobre el allowlist real de esta máquina
def test_el_allowlist_de_esta_maquina_tiene_los_parametros(monkeypatch):
    monkeypatch.delenv("BIOQL_STUDIO_HOME", raising=False)
    f = al.path_file()
    if not f.exists():
        pytest.skip(f"no hay allowlist del dueño en {f}")
    es = al.load(f)
    con = [n for n, e in es.items() if e.get("params_seen")]
    assert len(es) > 100, f"sólo {len(es)} entradas: nada que concluir"
    assert len(con) > 150, f"sólo {len(con)} entradas con parámetros leídos"
    # y los tres que verifiqué a mano contra el fuente de BioQL
    assert set(es["render_2d"]["params_seen"]) >= {"smiles", "width", "height", "format"}
    assert "pdb_text" in es["amp_bms_md"]["params_seen"]
