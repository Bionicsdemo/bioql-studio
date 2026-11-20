"""Las etapas vivas: el emparejamiento de los dos ids, y que nunca se invente una etapa.

Los cuerpos de aqui son los MEDIDOS en una corrida viva de PEPTIDE_DISCOVERY el 2026-11-19 a
las 18:21 (job `9df671b5-931`, pipeline `deade7a0-509`, target PD-L1), no inventados: es el
caso que el modulo existe para resolver.
"""
from __future__ import annotations

import time

import pytest

from studio import stages

JOB = {"job_id": "9df671b5-931", "target": "PD-L1", "status": "running",
       "created_at": "2026-11-19T18:21:04.453503",
       "progress": "Executing pipeline on 127.0.0.1:5035..."}

ACTIVE = {"active_pipelines": {"deade7a0-509": {
    "pipeline_id": "deade7a0-509", "pipeline_type": "PEPTIDE_DISCOVERY", "target": "PD-L1",
    "status": "running", "current_stage": "ADMET (candidate 1)", "current_status": "started",
    "completed_stages": 6, "total_stages": 11,
    "start_time": "2026-11-19T18:21:04.712028", "last_update": "2026-11-19T18:22:35.693093",
    "current_details": {"variants": 3},
    "stages": [
        {"stage": "Target Analysis", "status": "completed",
         "started_at": "2026-11-19T18:21:42.564553", "completed_at": "2026-11-19T18:21:43.253840"},
        {"stage": "ADMET (candidate 1)", "status": "running",
         "started_at": "2026-11-19T18:22:35.693093", "completed_at": None},
    ]}}}


def test_empareja_la_corrida_real_medida():
    pid, why = stages.pipeline_of(ACTIVE, JOB["target"], JOB["created_at"])
    assert pid == "deade7a0-509"
    assert "PD-L1".casefold() in why


def test_dos_corridas_iguales_a_la_vez_no_se_adivinan():
    """Elegir una pondria las etapas de OTRA corrida en la linea de este trabajo."""
    dos = {"active_pipelines": dict(ACTIVE["active_pipelines"])}
    dos["active_pipelines"]["otra-999"] = {
        **ACTIVE["active_pipelines"]["deade7a0-509"],
        "pipeline_id": "otra-999", "start_time": "2026-11-19T18:21:05.000000"}
    pid, why = stages.pipeline_of(dos, JOB["target"], JOB["created_at"])
    assert pid is None
    assert "no se puede decidir" in why


def test_otra_diana_no_empareja():
    pid, why = stages.pipeline_of(ACTIVE, "KRAS", JOB["created_at"])
    assert pid is None and "kras" in why.casefold()


def test_fuera_de_la_ventana_no_empareja():
    """Una corrida de hace una hora sobre la misma diana no es esta."""
    pid, _ = stages.pipeline_of(ACTIVE, "PD-L1", "2026-11-19T17:00:00.000000")
    assert pid is None


def test_sin_pipelines_activos_es_ausencia_con_causa():
    pid, why = stages.pipeline_of({"active_pipelines": {}}, "PD-L1", JOB["created_at"])
    assert pid is None and "ningun pipeline activo" in why


@pytest.mark.parametrize("bad", [None, {}, {"active_pipelines": None}, {"active_pipelines": [1, 2]}, "texto"])
def test_un_cuerpo_raro_no_revienta(bad):
    pid, why = stages.pipeline_of(bad, "PD-L1", JOB["created_at"])
    assert pid is None and why


def test_created_at_ilegible_no_empareja_con_1970():
    """Una fecha que no se puede leer debe ser None, nunca 0: 0 cae en 1970 y la ventana
    [1970-10s, 1970+180s] no emparejaria nada... salvo un start_time igual de roto."""
    assert stages._epoch("no es una fecha") is None
    assert stages._epoch(None) is None
    assert stages._epoch("") is None
    pid, why = stages.pipeline_of(ACTIVE, "PD-L1", "ayer por la tarde")
    assert pid is None and "created_at" in why


def test_target_como_objeto_tambien_empareja():
    """BioQL escribe la diana a veces como objeto."""
    pid, _ = stages.pipeline_of(ACTIVE, {"name": "PD-L1"}, JOB["created_at"])
    assert pid == "deade7a0-509"


def test_el_parte_copia_lo_que_bioql_dice_y_nada_mas():
    snap = stages.snapshot(ACTIVE["active_pipelines"]["deade7a0-509"], "deade7a0-509", "prueba")
    assert snap["completed_stages"] == 6 and snap["total_stages"] == 11
    assert snap["current_stage"] == "ADMET (candidate 1)"
    assert [s["status"] for s in snap["stages"]] == ["completed", "running"]
    assert snap["matched_by"] == "prueba"
    # Un campo que BioQL no dijo queda AUSENTE, no en cero ni en cadena vacia.
    assert "molecule_name" not in snap
    assert "error" not in snap


def test_el_parte_de_un_cuerpo_vacio_no_fabrica_totales():
    snap = stages.snapshot({}, "p", "prueba")
    assert snap["stages"] == []
    assert "total_stages" not in snap and "completed_stages" not in snap


def test_sin_etapas_localizables_lleva_la_causa():
    u = stages.unavailable("ningun pipeline activo")
    assert u["stages"] == [] and u["stages_unavailable"] == "ningun pipeline activo"


def test_publicar_leer_y_borrar(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOQL_STUDIO_HOME", str(tmp_path))
    snap = stages.snapshot(ACTIVE["active_pipelines"]["deade7a0-509"], "deade7a0-509", "prueba")
    stages.publish("9df671b5-931", snap)
    assert stages.read("9df671b5-931")["pipeline_id"] == "deade7a0-509"
    assert "9df671b5-931" in stages.read()
    stages.publish("9df671b5-931", None)
    assert stages.read("9df671b5-931") == {}
    assert stages.read() == {}


def test_un_parte_caducado_no_se_sirve(tmp_path, monkeypatch):
    """Una barra a medias de algo que murio hace horas es peor que ninguna barra."""
    monkeypatch.setenv("BIOQL_STUDIO_HOME", str(tmp_path))
    viejo = stages.snapshot(ACTIVE["active_pipelines"]["deade7a0-509"], "deade7a0-509", "prueba")
    viejo["at"] = time.time() - stages.STALE_S - 60
    stages.publish("viejo", viejo)
    assert stages.read("viejo") == {}


def test_el_fichero_es_privado(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOQL_STUDIO_HOME", str(tmp_path))
    stages.publish("j", stages.unavailable("x"))
    assert oct(stages.path_file().stat().st_mode & 0o777) == "0o600"
