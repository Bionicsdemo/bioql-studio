"""Los parámetros que BioQL lee de verdad, sacados de su propio código.

**El problema, medido.** De las 502 entradas del allowlist, **386 no declaran un solo parámetro**: el registro
de BioQL (`/api/tools/registry`) publica 63 herramientas y ninguna más. Para las otras 386 Studio conoce la ruta
y no el contrato, así que Claude tiene que adivinar los nombres — y una llamada con `target=` donde el handler
lee `protein=` vuelve con un 400 que se lee como que BioQL está roto.

Esto lo arregla por el único camino honesto: **leyendo los handlers**. Un `data.get("smiles")` dentro del
manejador de `/api/quantum/qubo_solve` ES la declaración de que esa ruta toma `smiles`, la escribiera alguien en
un registro o no.

**Precisión por encima de cobertura.** No se buscan todos los `.get(` del fichero: eso traería `os.environ.get`,
`result.get` y medio mundo. Se localiza primero QUÉ VARIABLE sostiene el cuerpo de la petición en ese handler
(`data = request.get_json()`, `payload = request.json or {}`) y sólo se sigue esa, más los accesos directos a
`request.args` / `request.form`. Lo que no se pueda atribuir con esa regla no se inventa.

**Lo que esto NO hace.** No toca `required` ni `optional`, y no cambia `schema_known`. Esos campos gobiernan a
quién se le rechaza una llamada, y un parámetro deducido de una lectura no es una declaración: marcar como
obligatorio algo que no lo es rompería llamadas que hoy funcionan, y añadirlo a `optional` haría que el
adaptador empezara a refusar lo que no está en la lista. Va a un campo propio, `params_seen`, que sólo se usa
para CONTAR lo que la herramienta dice de sí misma en su descripción.

Uso:
    python3 tools/discover_params.py                 # informe, no escribe nada
    python3 tools/discover_params.py --write         # lo guarda en el allowlist del dueño
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

BACKEND = Path("/Users/heinzjungbluth/Documents/Proyectos/Spectrix_BioQL/src/backend")

ROUTE = re.compile(r'@\w+\.route\(\s*[\'"]([^\'"]+)[\'"]')
# `data = request.get_json(...)`, `payload = request.json or {}`, `d = request.get_json(silent=True) or {}`
BODY_VAR = re.compile(r'^\s*(\w+)\s*=\s*(?:await\s+)?request\.(?:get_json\s*\(|json\b)')
DIRECT = re.compile(r'request\.(?:args|form|values)\.get\(\s*[\'"]([A-Za-z_][\w\-]*)[\'"]')
# los duplicados de iCloud (" 2.py") son copias viejas y contaminan cualquier recuento
DUP = re.compile(r" \d+\.py$")


def _handler_body(lines: list[str], start: int, limit: int = 500) -> list[str]:
    """Del decorador hasta el siguiente decorador de ruta, o 500 líneas."""
    end = len(lines)
    for j in range(start + 1, min(start + limit, len(lines))):
        if ROUTE.search(lines[j]):
            end = j
            break
    else:
        end = min(start + limit, len(lines))
    return lines[start:end]


def params_of(body: list[str]) -> set[str]:
    found: set[str] = set()
    varnames: set[str] = set()
    for l in body:
        m = BODY_VAR.match(l)
        if m:
            varnames.add(m.group(1))
        found |= set(DIRECT.findall(l))
    for v in varnames:
        getter = re.compile(rf'\b{re.escape(v)}\s*\.get\(\s*[\'"]([A-Za-z_][\w\-]*)[\'"]')
        item = re.compile(rf'\b{re.escape(v)}\s*\[\s*[\'"]([A-Za-z_][\w\-]*)[\'"]\s*\]')
        for l in body:
            found |= set(getter.findall(l))
            found |= set(item.findall(l))
    # nombres que no son parámetros de ciencia sino fontanería del propio servidor
    return {f for f in found if f not in {"_pipeline_session_id", "__debug", "_internal"}}


def scan() -> dict[str, list[str]]:
    """ruta -> parámetros vistos, sobre todo el backend."""
    out: dict[str, set[str]] = {}
    for py in BACKEND.rglob("*.py"):
        if DUP.search(py.name) or "/tests/" in str(py):
            continue
        try:
            lines = py.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, l in enumerate(lines):
            m = ROUTE.search(l)
            if not m:
                continue
            ruta = m.group(1)
            ps = params_of(_handler_body(lines, i))
            if ps:
                out.setdefault(ruta, set()).update(ps)
    return {k: sorted(v) for k, v in out.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="guardar en el allowlist del dueño")
    ns = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    os.environ.pop("BIOQL_STUDIO_HOME", None)
    from studio import allowlist as al

    vistos = scan()
    es = al.load()
    con, sin, nuevos = 0, 0, 0
    cambios: dict[str, list[str]] = {}
    for name, e in es.items():
        ps = vistos.get(e["path"])
        if not ps:
            sin += 1
            continue
        con += 1
        ya = set(e.get("required") or []) | set(e.get("optional") or [])
        extra = [p for p in ps if p not in ya]
        if extra:
            nuevos += len(extra)
            cambios[name] = ps

    print(f"rutas del backend con parámetros legibles: {len(vistos)}")
    print(f"entradas del allowlist que los reciben:    {con}  (sin: {sin})")
    print(f"parámetros nuevos que nadie declaraba:     {nuevos}")
    print("\nejemplos:")
    for n in list(cambios)[:10]:
        print(f"   {n:38s} {cambios[n][:9]}")

    if not ns.write:
        print("\n(informe solo; con --write se guarda)")
        return 0

    f = al.path_file()
    raw = json.loads(f.read_text()) if f.exists() else {}
    for name, ps in cambios.items():
        raw.setdefault(name, {})
        raw[name]["params_seen"] = ps
    f.write_text(json.dumps(raw, indent=1, sort_keys=True, ensure_ascii=False))
    print(f"\nguardado en {f}: {len(cambios)} entrada(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
