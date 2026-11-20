"""Las etapas vivas de un pipeline de BioQL, para que la ventana las dibuje como lo hace la UI de :3000.

POR QUE ESTE MODULO EXISTE. Una corrida de BioQL tiene DOS ids y nada los conecta:

- el submit responde con un `job_id` (`9df671b5-931`), que es lo UNICO que Studio llega a saber;
- la lista de etapas vive bajo un `pipeline_id` DISTINTO (`deade7a0-509`), en
  `/api/pipeline/progress/<pipeline_id>`, que es lo que sondea el frontend de :3000
  (`PipelineProgressTracker.tsx:62-97`, cada 3 s).

Medido sobre una corrida viva de PEPTIDE_DISCOVERY el 2026-11-19 a las 18:21:

    GET /api/pipeline/progress/deade7a0-509  -> 200, 11 etapas, 6 hechas, target "PD-L1"
    GET /api/pipeline/job/deade7a0-509       -> 404 {"error": "Job not found"}
    GET /api/pipeline/job/9df671b5-931       -> 200, target "PD-L1", status "running",
                                                progress "Executing pipeline on 127.0.0.1:5035..."

`deade7a0-509` no aparece en `/api/pipeline/jobs`. El cuerpo de progreso no trae `job_id` y el
del trabajo no trae `pipeline_id`. Por eso el adaptador, que solo conoce el `job_id`, sondeaba
el espacio de ids equivocado y se quedaba con un `progress` de texto fijo: no es que tirara las
etapas, es que no podia encontrarlas.

COMO SE SALVA. El id se EMPAREJA, no se deriva. `/api/pipeline/active` lista cada pipeline en
curso con su `target` y su `start_time`, y el trabajo lleva `target` y `created_at`. En la
corrida medida iban a 259 ms uno del otro (18:21:04.453 el trabajo, 18:21:04.712 el pipeline).

Exactamente UN candidato dentro de la ventana es la pareja. Cero es ABSENT: todavia no hay
etapas, o esa ruta no registra ninguna. Mas de uno es UNRESOLVABLE — dos corridas sobre la misma
diana en el mismo segundo no se pueden distinguir, y adivinar ahi pondria las etapas de otra
corrida en la linea de este trabajo. Nunca se inventa una etapa: si la pareja no es unica el
parte lo dice y no lleva etapas.

El arreglo de verdad es de una linea en el backend: que el submit o el cuerpo del trabajo
devuelvan el `pipeline_id`. Entonces `pipeline_of()` sobra y este modulo se queda solo con el
parte. Mientras no este, esto es lo que hay, y dice en `matched_by` como lo supo.

DOS PROCESOS. La ventana y el servidor MCP son procesos distintos (ver `ledger.append`), asi que
el parte no puede vivir en memoria: va a un fichero con flock, como el cuaderno.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from datetime import datetime
from pathlib import Path

from . import home, write_private

# El pipeline arranca DESPUES de crearse el trabajo, pero los relojes de las dos anotaciones no
# son el mismo, asi que la ventana admite un poco de holgura por detras. 180 s por delante cubre
# un arranque lento (cola, carga de modelo) sin llegar a abarcar una corrida siguiente.
BEFORE_S = 10.0
AFTER_S = 180.0
# Un parte mas viejo que esto es basura de una corrida muerta: no se sirve ni se guarda.
STALE_S = 6 * 3600.0


def path_file() -> Path:
    return home() / "running.json"


def _epoch(ts: object) -> float | None:
    """ISO 8601 sin zona, como lo escribe BioQL (`2026-11-19T18:21:04.453503`). Un valor que no
    se puede leer es None — nunca `0`, que emparejaria con cualquier cosa de 1970."""
    if not isinstance(ts, str) or not ts.strip():
        return None
    try:
        return datetime.fromisoformat(ts.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _norm(target: object) -> str:
    """BioQL escribe la diana a veces como cadena y a veces como objeto con `name`."""
    if isinstance(target, dict):
        target = target.get("name") or target.get("gene") or ""
    return str(target or "").strip().casefold()


def pipeline_of(active: object, target: object, created_at: object) -> tuple[str | None, str]:
    """El `pipeline_id` que corresponde a este trabajo, o None con la razon.

    `active` es el cuerpo de `/api/pipeline/active`. Devuelve `(pipeline_id, matched_by)`.
    `matched_by` viaja hasta el parte: quien lea una etapa debe poder ver COMO se decidio que
    era de este trabajo.
    """
    want = _norm(target)
    born = _epoch(created_at)
    if not want:
        return None, "el trabajo no dice target: no hay con que emparejar"
    if born is None:
        return None, f"el trabajo no trae created_at legible ({created_at!r}): no hay ventana"
    pipes = (active or {}).get("active_pipelines") if isinstance(active, dict) else None
    if not isinstance(pipes, dict) or not pipes:
        return None, "ningun pipeline activo"

    hits = []
    for pid, p in pipes.items():
        if not isinstance(p, dict):
            continue
        if _norm(p.get("target")) != want:
            continue
        began = _epoch(p.get("start_time"))
        if began is None or not (born - BEFORE_S <= began <= born + AFTER_S):
            continue
        hits.append(str(p.get("pipeline_id") or pid))

    if len(hits) == 1:
        return hits[0], f"target {want!r} + arranque dentro de la ventana"
    if not hits:
        return None, f"ningun pipeline activo sobre {want!r} arrancado junto a este trabajo"
    # Dos corridas iguales a la vez: elegir una pondria las etapas de otra en esta linea.
    return None, f"{len(hits)} pipelines sobre {want!r} en la misma ventana: no se puede decidir"


def snapshot(progress: object, pipeline_id: str, matched_by: str) -> dict:
    """Lo que la ventana dibuja, sacado del cuerpo de `/api/pipeline/progress/<pipeline_id>`.

    Se copian los campos que usa `PipelineProgressTracker.tsx`; lo que BioQL no diga queda
    ausente, no en cero. `total_stages` en 0 con `completed_stages` en 6 daria una barra al
    600%, asi que el porcentaje no se calcula aqui: lo hace la ventana, que sabe si puede.
    """
    p = progress if isinstance(progress, dict) else {}
    stages = [
        {"stage": str(s.get("stage") or ""), "status": str(s.get("status") or ""),
         "started_at": s.get("started_at"), "completed_at": s.get("completed_at")}
        for s in (p.get("stages") or []) if isinstance(s, dict)
    ]
    snap = {
        "pipeline_id": pipeline_id,
        "matched_by": matched_by,
        "at": time.time(),
        "stages": stages,
    }
    for k in ("pipeline_type", "target", "status", "current_stage", "current_details",
              "completed_stages", "total_stages", "start_time", "last_update", "error"):
        if k in p:
            snap[k] = p[k]
    return snap


def unavailable(why: str) -> dict:
    """Un parte sin etapas, con la causa. Es informacion: dice que la corrida sigue viva y que
    las etapas no se pudieron localizar, que no es lo mismo que un pipeline sin etapas."""
    return {"at": time.time(), "stages": [], "stages_unavailable": why}


def _load(f: Path) -> dict:
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _held(f: Path) -> int:
    """flock sobre un fichero propio, como hace el cuaderno: la ventana y el servidor MCP
    escriben aqui desde procesos distintos. Devuelve el descriptor, que el llamante cierra."""
    f.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = os.open(str(f) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return lock
    except OSError:
        os.close(lock)
        raise


def publish(job_id: str, snap: dict | None) -> None:
    """Dejar (o quitar, con None) el parte de un trabajo. Un fallo aqui NUNCA cuesta la llamada:
    el progreso es un adorno, el resultado cientifico no depende de el."""
    f = path_file()
    try:
        lock = _held(f)
    except OSError:
        return
    try:
        cur = _load(f)
        now = time.time()
        cur = {k: v for k, v in cur.items()
               if isinstance(v, dict) and now - float(v.get("at") or 0) < STALE_S}
        if snap is None:
            cur.pop(job_id, None)
        else:
            cur[job_id] = snap
        write_private(f, json.dumps(cur, ensure_ascii=False))
    except (OSError, ValueError, TypeError):
        return
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)


def read(job_id: str | None = None) -> dict:
    """Los partes vivos. Con `job_id`, solo ese (o `{}`). Lo caducado no se sirve: un parte de
    hace horas dibujaria una barra a medias de algo que ya murio."""
    cur = _load(path_file())
    now = time.time()
    live = {k: v for k, v in cur.items()
            if isinstance(v, dict) and now - float(v.get("at") or 0) < STALE_S}
    if job_id is not None:
        return live.get(job_id, {})
    return live
