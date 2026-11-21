"""Nada escribe dentro del paquete firmado, y la instalación lo comprueba.

El 2026-11-21 por la mañana la app dejó de abrirse. El motivo no estaba en su código:

    $ codesign --verify --deep --strict "/Applications/BioQL Studio.app"
    /Applications/BioQL Studio.app: a sealed resource is missing or invalid
    file added: …/Resources/bioql-studio/studio/__pycache__/mcp_server.cpython-314.pyc

Trece ficheros `.pyc` dentro del paquete. Una firma de macOS sella la LISTA de ficheros, así que uno AÑADIDO la
invalida igual que uno modificado. Los escribió el servidor MCP: corre con `cwd` y `PYTHONPATH` dentro del
paquete instalado (así está configurado en Claude Desktop y en Claude Code), y Python escribe su bytecode al
lado del fuente. Borrarlos devolvió el sello a «valid on disk» sin volver a firmar nada, y la app abrió.

Es decir: **el uso normal de la app rompía la app**. Y la segunda mitad del defecto era la instalación a mano,
copiando encima: una copia que no borra lo viejo deja sobrevivir lo que hubiera allí, y la firma nueva no lo
cubre.

Se cierra tres veces, a propósito, porque dos de los cierres dependen de que algo externo siga bien:

1. `studio/__init__.py` apaga la escritura de bytecode al verse dentro de un `.app`, y borra el que ya hubiera
   —incluido el suyo, que se escribe antes de que esa línea pueda correr—; pero depende de que el proceso pase
   por este paquete, y un `python -c "import app.service"` no pasa;
2. `PYTHONDONTWRITEBYTECODE` en la configuración MCP — depende de un fichero de configuración del dueño;
3. la app barre el bytecode de sus Recursos al arrancar (`Service.sweepBytecode`) y `install.sh` borra antes de
   copiar y verifica el sello — esto no depende de nada.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ 1. el paquete, dentro de un .app
def test_dentro_de_un_app_no_se_escribe_bytecode(tmp_path):
    """EL test. No comprueba el texto del fichero: monta un `.app` de verdad, importa desde dentro y mira si
    quedó un `__pycache__`."""
    dentro = tmp_path / "Falsa.app" / "Contents" / "Resources" / "bioql-studio" / "studio"
    dentro.mkdir(parents=True)
    shutil.copy(RAIZ / "studio" / "__init__.py", dentro / "__init__.py")
    raiz = dentro.parent
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); import studio; print(sys.dont_write_bytecode)",
         str(raiz)],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "True", out.stdout
    assert not list(dentro.glob("__pycache__")), "quedó bytecode dentro del paquete"


def test_fuera_de_un_app_se_sigue_escribiendo(tmp_path):
    """Y no se apaga en el repo: aquí el bytecode es velocidad y no rompe ninguna firma."""
    fuera = tmp_path / "repo" / "studio"
    fuera.mkdir(parents=True)
    shutil.copy(RAIZ / "studio" / "__init__.py", fuera / "__init__.py")
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); import studio; print(sys.dont_write_bytecode)",
         str(fuera.parent)],
        capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "False", out.stdout


# ------------------------------------------------------------------ 2. la app se barre a sí misma
def test_la_app_barre_el_bytecode_de_sus_recursos_antes_de_arrancar():
    sw = (RAIZ / "app" / "macos" / "Sources" / "Service.swift").read_text()
    assert "func sweepBytecode(in root: URL)" in sw
    # y se llama ANTES de lanzar el hijo, no después
    i, j = sw.index("sweepBytecode(in: root)"), sw.index('p.arguments = ["-m", "app", "serve"')
    assert i < j, "barrer después de arrancar no repara el sello de este arranque"
    assert '"__pycache__"' in sw and '"pyc"' in sw


# ------------------------------------------------------------------ 3. la instalación
def test_la_instalacion_borra_antes_de_copiar():
    sh = (RAIZ / "app" / "macos" / "install.sh").read_text()
    i, j = sh.index('rm -rf "$DEST"'), sh.index('ditto "$SRC" "$DEST"')
    assert i < j, "copiar encima deja sobrevivir lo que hubiera dentro"


def test_la_instalacion_refusa_bytecode_y_verifica_el_sello():
    sh = (RAIZ / "app" / "macos" / "install.sh").read_text()
    assert "exit 1" in sh.split("rompe el sello")[0], "ve bytecode y no falla"
    assert 'codesign --verify --deep --strict "$DEST"' in sh
    assert "set -euo pipefail" in sh, "sin esto un paso que falla no para la instalación"


def test_la_construccion_nunca_mete_bytecode_en_lo_que_firma():
    b = (RAIZ / "app" / "macos" / "build.sh").read_text()
    assert "--exclude '__pycache__'" in b and "--exclude '*.pyc'" in b
    i, j = b.index("rsync -a --exclude '__pycache__'"), b.index("codesign --force --deep --sign -")
    assert i < j, "se firma antes de copiar: la firma no cubriría lo copiado"


# ------------------------------------------------------------------ 4. la configuración del dueño
def test_la_configuracion_mcp_de_esta_maquina_apaga_el_bytecode():
    """Sobre los ficheros reales. Si alguien los reescribe sin la variable, esto lo dice — y los otros dos
    cierres siguen aguantando."""
    import json

    import pytest
    faltan = []
    for f in (Path.home() / ".claude.json",
              Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"):
        if not f.exists():
            continue
        srv = json.loads(f.read_text()).get("mcpServers", {}).get("bioql")
        if not srv:
            continue
        dentro = ".app/Contents/" in str(srv.get("cwd", "")) or ".app/Contents/" in str(
            srv.get("env", {}).get("PYTHONPATH", ""))
        if dentro and srv.get("env", {}).get("PYTHONDONTWRITEBYTECODE") != "1":
            faltan.append(f.name)
    if not faltan:
        return
    pytest.fail("servidor MCP apuntando dentro del paquete y sin PYTHONDONTWRITEBYTECODE: " + ", ".join(faltan))
