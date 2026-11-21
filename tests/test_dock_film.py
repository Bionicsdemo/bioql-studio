"""La película: el acercamiento trazado, fotograma a fotograma.

Lo que estos tests sujetan, y por qué cada uno:

- **Una película corta no pasa por entera.** BioQL tiene su propia ruta de animación y, cuando sus 8 fotogramas
  fallaron los 8, contestó `{"error": "No frames rendered"}` con la causa tragada. Aquí, pedir 12 y rendir 9 es
  UNRESOLVABLE con los dos números. Ese es el fallo que no se repite.
- **Nada del que llama entra en el guion.** El lenguaje de PyMOL abre ficheros, los escribe y ejecuta Python. El
  guion es texto fijo de `dock_film.py` y lo que varía viaja en un JSON que el guion lee.
- **Los tres estados.** Sin PyMOL en este Mac es ABSENT (una respuesta sobre la máquina); PyMOL que revienta o
  cuelga es UNRESOLVABLE. Nunca una película vacía ni una foto fija haciéndose pasar por una.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from studio import dock_film
from studio.resolution import ABSENT, RESOLVED, UNRESOLVABLE

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "app" / "web" / "app.js").read_text()

# Una estructura mínima y real: tres átomos de un péptido, suficiente para que PyMOL cargue algo.
TINY = (
    "ATOM      1  N   ALA A   1      11.104   6.134  -6.504  1.00 20.00           N\n"
    "ATOM      2  CA  ALA A   1      11.639   6.071  -5.147  1.00 20.00           C\n"
    "ATOM      3  C   ALA A   1      13.140   6.209  -5.153  1.00 20.00           C\n"
    "END\n"
)


# ------------------------------------------------------------------ lo que ni se intenta
def test_sin_estructura_es_absent():
    r = dock_film.film("", "pdb")
    assert r.state == ABSENT and "no structure" in r.detail


def test_un_formato_que_pymol_no_lee_es_absent():
    r = dock_film.film(TINY, "cuaderno")
    assert r.state == ABSENT and "cuaderno" in r.detail


def test_una_estructura_enorme_se_dice_y_no_se_recorta():
    """Recortar una estructura la convierte en otra molécula; eso se dice, no se hace."""
    r = dock_film.film("A" * (dock_film.MAX_BYTES + 1), "pdb")
    assert r.state == ABSENT
    assert "not cut down" in r.detail and "different molecule" in r.detail


def test_sin_pymol_en_este_mac_es_absent_no_un_fallo(monkeypatch):
    monkeypatch.setattr(dock_film, "binary", lambda: None)
    r = dock_film.film(TINY, "pdb")
    assert r.state == ABSENT and "brew install pymol" in r.detail


def test_un_ligando_en_un_formato_que_no_se_escribe_tambien_es_absent(monkeypatch):
    monkeypatch.setattr(dock_film, "binary", lambda: "/bin/echo")
    r = dock_film.film(TINY, "pdb", "algo", "cuaderno")
    assert r.state == ABSENT and "cuaderno" in r.detail


# ------------------------------------------------------------------ el guion no lo escribe quien llama
def test_el_guion_es_texto_fijo_y_no_interpola_nada():
    """Si alguien mete un `.format(`, un f-string o un `%` sobre el guion, esto cae. El lenguaje de PyMOL puede
    abrir ficheros, escribirlos y correr Python: un guion moldeado por quien llama sería una consola."""
    src = (ROOT / "studio" / "dock_film.py").read_text()
    m = re.search(r"SCRIPT = r'''(.*?)'''", src, re.S)
    assert m, "el guion dejó de ser una cadena cruda de este fichero"
    guion = m.group(1)
    # El guion usa `%` sobre sus PROPIAS variables de bucle, que es inofensivo. Lo que importa es que el módulo
    # no lo formatee con nada: se escribe tal cual, y lo que varía entra por el JSON.
    assert "script.write_text(SCRIPT" in src, "el guion dejó de escribirse tal cual"
    for prohibido in ("SCRIPT %", "SCRIPT.format", "SCRIPT.replace", f'{"{"}SCRIPT{"}"}'):
        assert prohibido not in src, f"el guion se construye con {prohibido}"
    assert "cmd.load(cfg[" in guion and 'json.dumps({' in src and '"film.json"' in src


def test_la_estructura_entra_como_fichero_y_nunca_dentro_del_guion():
    src = (ROOT / "studio" / "dock_film.py").read_text()
    assert "p.write_text(data" in src, "la estructura ya no se escribe a disco"
    m = re.search(r"SCRIPT = r'''(.*?)'''", src, re.S)
    assert "cmd.load(cfg[" in m.group(1), "el guion ya no carga desde la configuración"


# ------------------------------------------------------------------ una película con agujeros no pasa
def _fake_pymol(frames_escritos: int, exit_code: int = 0, stderr: str = ""):
    """Hace de PyMOL: lee `film.json` del directorio de trabajo y escribe tantos fotogramas como se le diga."""
    def run(cmd_args, **kw):
        work = Path(kw.get("cwd") or ".")
        cfg = json.loads((work / "film.json").read_text())
        out = Path(cfg["out_dir"])
        said = []
        for i in range(frames_escritos):
            (out / f"frame_{i:03d}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 40)
            said.append(f"FILMFRAME {i} ok")
        return subprocess.CompletedProcess(cmd_args, exit_code, "\n".join(said) + "\n", stderr)
    return run


def test_menos_fotogramas_de_los_pedidos_es_unresolvable_con_los_dos_numeros(monkeypatch):
    """EL test. Es el fallo exacto que la ruta de BioQL escondió detrás de 'No frames rendered'."""
    monkeypatch.setattr(dock_film, "binary", lambda: "/bin/echo")
    monkeypatch.setattr(dock_film.subprocess, "run", _fake_pymol(9))
    r = dock_film.film(TINY, "pdb", n_frames=12)
    assert r.state == UNRESOLVABLE
    assert "12 frames asked for" in r.detail and "9 rendered" in r.detail


def test_ningun_fotograma_dice_la_ultima_linea_de_pymol(monkeypatch):
    monkeypatch.setattr(dock_film, "binary", lambda: "/bin/echo")
    monkeypatch.setattr(dock_film.subprocess, "run", _fake_pymol(0, 1, "Segmentation fault"))
    r = dock_film.film(TINY, "pdb", n_frames=6)
    assert r.state == UNRESOLVABLE
    assert "no frame" in r.detail and "Segmentation fault" in r.detail


def test_un_pymol_colgado_es_unresolvable_con_su_tiempo(monkeypatch):
    def cuelga(*a, **k):
        raise subprocess.TimeoutExpired(cmd="pymol", timeout=dock_film.TIMEOUT)
    monkeypatch.setattr(dock_film, "binary", lambda: "/bin/echo")
    monkeypatch.setattr(dock_film.subprocess, "run", cuelga)
    r = dock_film.film(TINY, "pdb", n_frames=4)
    assert r.state == UNRESOLVABLE and "did not finish" in r.detail


def test_el_numero_de_fotogramas_se_acota_por_los_dos_lados(monkeypatch):
    visto = {}
    def run(cmd_args, **kw):
        # `version()` entra por aquí también y no lleva `cwd`: devolverle una versión es lo que haría PyMOL.
        if not kw.get("cwd") or not (Path(kw["cwd"]) / "film.json").is_file():
            return subprocess.CompletedProcess(cmd_args, 0, "VER 3.1.0\n", "")
        cfg = json.loads((Path(kw["cwd"]) / "film.json").read_text())
        visto["n"] = cfg["n_frames"]
        return _fake_pymol(cfg["n_frames"])(cmd_args, **kw)
    monkeypatch.setattr(dock_film, "binary", lambda: "/bin/echo")
    monkeypatch.setattr(dock_film.subprocess, "run", run)
    dock_film.film(TINY, "pdb", n_frames=1)
    assert visto["n"] == 2, "menos de dos fotogramas no es una película"
    dock_film.film(TINY, "pdb", n_frames=500)
    assert visto["n"] == dock_film.FRAMES_MAX


def test_una_pelicula_entera_lleva_su_procedencia(monkeypatch):
    monkeypatch.setattr(dock_film, "binary", lambda: "/bin/echo")
    monkeypatch.setattr(dock_film.subprocess, "run", _fake_pymol(6))
    monkeypatch.setattr(dock_film, "version", lambda exe=None: __import__("studio.resolution", fromlist=["resolved"]).resolved("3.1.0", "pymol", "x"))
    r = dock_film.film(TINY, "pdb", n_frames=6)
    assert r.state == RESOLVED
    v = r.value
    assert v["n_frames"] == 6 and len(v["frames"]) == 6
    assert v["pymol"] == "3.1.0" and v["per_frame"] >= 0 and v["has_ligand"] is False
    assert "6 frames" in r.detail


# ------------------------------------------------------------------ con el PyMOL de verdad, si lo hay
@pytest.mark.skipif(dock_film.binary() is None, reason="no hay PyMOL en este Mac")
def test_pymol_de_verdad_traza_dos_fotogramas():
    r = dock_film.film(TINY, "pdb", n_frames=2, width=240, height=180)
    assert r.state == RESOLVED, r.detail
    assert len(r.value["frames"]) == 2
    import base64
    for f in r.value["frames"]:
        assert base64.b64decode(f)[:8] == b"\x89PNG\r\n\x1a\n", "eso no es un PNG"


# ------------------------------------------------------------------ lo que hace la ventana con ello
def test_la_ventana_recorre_fotogramas_y_no_reproduce_un_gif():
    """Un GIF se reproduce y punto: no para en el fotograma 14 ni va hacia atrás, que es lo único que uno quiere
    mirar de un acercamiento. Si alguien lo cambia por un GIF, esto cae."""
    assert "function filmPlayer(" in JS
    assert "image/gif" not in JS, "la ventana volvió a un GIF"
    assert 'time.type = "range"' in JS, "la película perdió su línea de tiempo"
    for tecla in ("ArrowRight", "ArrowLeft"):
        assert tecla in JS, f"no se puede pasar fotograma con {tecla}"


def test_la_ventana_dice_de_donde_salio_la_pelicula():
    """Una imagen sin procedencia es un dibujo (render3d.py). La película dice PyMOL, cuántos fotogramas, cuánto
    costó cada uno, y si hubo acercamiento o solo una órbita."""
    assert "fotogramas · ${v.per_frame}s cada uno" in JS
    assert "órbita (sin ligando aparte)" in JS


def test_la_ruta_de_la_pelicula_no_entra_en_el_registro_firmado():
    """El registro es de llamadas a BioQL. Esto es PyMOL local sobre una estructura que la ventana ya tiene: no
    gasta el motor de BioQL y no toca el 5035, igual que /api/render3d."""
    src = (ROOT / "app" / "service.py").read_text()
    i = src.index('if p == "/api/dockfilm"')
    trozo = src[i:i + 900]
    assert "registro firmado" in trozo and "no toca el 5035" in trozo
    assert "dock_film" in trozo


# ------------------------------------------------------------------ el fichero, si se puede
def test_sin_PIL_no_hay_gif_pero_si_pelicula(monkeypatch):
    """Studio no tiene dependencias: ni un `requirements.txt` ni un import fuera de la biblioteca estándar, y eso
    es una propiedad. Un GIF animado necesita cuantizar a paleta, así que PIL se importa DENTRO y en un try. Si
    no está, la película sigue entera y se dice por qué no hay fichero — una ausencia conocida no se convierte
    en un error."""
    import builtins
    real = builtins.__import__

    def sin_pil(name, *a, **k):
        if name.startswith("PIL"):
            raise ImportError("no PIL")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", sin_pil)
    monkeypatch.setattr(dock_film, "binary", lambda: "/bin/echo")
    monkeypatch.setattr(dock_film.subprocess, "run", _fake_pymol(4))
    monkeypatch.setattr(dock_film, "version", lambda exe=None: __import__(
        "studio.resolution", fromlist=["resolved"]).resolved("3.1.0", "pymol", "x"))
    r = dock_film.film(TINY, "pdb", n_frames=4)
    assert r.state == RESOLVED
    assert r.value["gif_path"] is None
    assert "PIL" in r.value["gif_why"] and "se recorre igual" in r.value["gif_why"]
    assert len(r.value["frames"]) == 4, "la película se perdió por culpa del GIF"


def test_un_gif_que_falla_no_tumba_la_pelicula(monkeypatch):
    monkeypatch.setattr(dock_film, "binary", lambda: "/bin/echo")
    monkeypatch.setattr(dock_film.subprocess, "run", _fake_pymol(3))
    monkeypatch.setattr(dock_film, "version", lambda exe=None: __import__(
        "studio.resolution", fromlist=["resolved"]).resolved("3.1.0", "pymol", "x"))
    # los PNG falsos de `_fake_pymol` no son imágenes válidas: PIL va a protestar, y eso es el caso de prueba
    r = dock_film.film(TINY, "pdb", n_frames=3)
    assert r.state == RESOLVED and len(r.value["frames"]) == 3
    assert r.value["gif_path"] is None and r.value["gif_why"]


@pytest.mark.skipif(dock_film.binary() is None, reason="no hay PyMOL en este Mac")
def test_con_pymol_de_verdad_sale_un_gif_animado():
    r = dock_film.film(TINY, "pdb", n_frames=3, width=200, height=150)
    assert r.state == RESOLVED, r.detail
    ruta = r.value.get("gif_path")
    if ruta is None:
        pytest.skip("este Python no tiene PIL: " + str(r.value.get("gif_why")))
    datos = Path(ruta).read_bytes()
    assert datos[:6] == b"GIF89a"
    assert b"NETSCAPE2.0" in datos, "no da la vuelta"
    # Un control por fotograma, PERO al menos uno: con `optimize=True` PIL colapsa los fotogramas identicos, y
    # tres atomos a 200x150 girando sesenta grados dan la misma imagen tres veces. Sobre una proteina de verdad
    # salen los seis (medido). Exigir exactamente N aqui seria exigir que el optimizador no optimice.
    controles = datos.count(b"\x00\x21\xF9\x04")
    assert 1 <= controles <= 3, controles


def test_la_ventana_dice_donde_quedo_el_fichero_o_por_que_no():
    assert "GIF guardado en" in JS
    assert "sin fichero GIF" in JS
