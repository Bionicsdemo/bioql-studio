"""La MD corta de BioQL: lo que devuelve es convergencia, no una película.

Se fue a buscar una trayectoria para reproducirla y `openmm_md_engine.run_short_md_trajectory` no devuelve
coordenadas: devuelve `per_frame_dg_kcal`, una MM-GBSA por fotograma, con media, desviación y el reparto por
réplicas. Así que lo que se dibuja es lo único que esos números pueden contestar — si convergió.

Lo que estos tests sujetan:

- **Las réplicas no se cosen.** Los fotogramas salen de simulaciones INDEPENDIENTES. Unirlos en una línea de
  tiempo diría que se avanzaron 30 ps cuando fueron tres de 10 ps que ni se conocen. El eje es el fotograma y las
  fronteras van marcadas.
- **Un hueco no se rellena.** Un fotograma sin número se cae y se NOMBRA; rellenarlo con la media sería una
  muestra que nadie tomó.
- **El descargo viaja visible.** El motor dice que esto no es energía libre convergida y que la desviación es una
  cota inferior, no una barra de error. Quien mira una media bonita tiene que leer eso en el mismo sitio.
"""
from __future__ import annotations

import math
from pathlib import Path

from studio import cards

JS = (Path(__file__).resolve().parent.parent / "app" / "web" / "app.js").read_text()

DISCLAIMER = (
    "Real short-trajectory MD (OpenMM ff14SB + GBn2 implicit solvent): minimization -> equilibration -> "
    "production, with MM-GBSA computed on multiple REAL frames sampled from the production trajectory "
    "(ensemble mean +/- std). This is a real but LIMITED-SAMPLING estimate from a short trajectory — it is NOT "
    "a converged free-energy result."
)


def payload(**over):
    """La forma que devuelve `run_short_md_trajectory`, con sus nombres."""
    base = {
        "available": True,
        "method": "short-MD-trajectory MM-GBSA (OpenMM ff14SB + GBn2, real dynamics)",
        "units": "kcal/mol",
        "per_frame_dg_kcal": [-31.2, -29.8, -30.5, -32.1, -30.9, -31.6],
        "dg_mean_kcal": -31.02,
        "dg_std_kcal": 0.79,
        "n_frames": 6,
        "frame_interval_ps": 2.0,
        "n_replicas_completed": 2,
        "replica_detail": [{"replica": 0, "n_frames": 3, "production_steps_completed": 5000},
                           {"replica": 1, "n_frames": 3, "production_steps_completed": 5000}],
        "production_ps_simulated_total": 20.0,
        "stopped_early": False,
        "stop_reason": None,
        "disclaimer": DISCLAIMER,
    }
    base.update(over)
    return base


def blocks(p):
    return cards.card(p, tool="openmm_md_trajectory")["blocks"]


def one(p, kind):
    return [b for b in blocks(p) if b.get("type") == kind]


# ------------------------------------------------------------------ lo que dibuja
def test_la_forma_de_la_md_se_reconoce_y_sale_un_bloque_de_convergencia():
    c = one(payload(), "converge")
    assert len(c) == 1
    b = c[0]
    assert b["unit"] == "kcal/mol"
    assert [p[1] for p in b["points"]] == [-31.2, -29.8, -30.5, -32.1, -30.9, -31.6]
    assert b["x_label"] == "fotograma", "el eje no puede ser tiempo: son réplicas independientes"
    assert b["mean"] == -31.02 and b["std"] == 0.79
    assert b["frame_interval_ps"] == 2.0


def test_la_media_corriente_es_la_media_corriente():
    """Es la señal de convergencia; si se calcula mal, el gráfico miente sobre lo único que contesta."""
    b = one(payload(), "converge")[0]
    dgs = [p[1] for p in b["points"]]
    esperado = [round(sum(dgs[:i + 1]) / (i + 1), 3) for i in range(len(dgs))]
    assert [p[1] for p in b["running"]] == esperado


def test_las_fronteras_entre_replicas_van_marcadas_y_son_acumuladas():
    b = one(payload(), "converge")[0]
    assert b["boundaries"] == [3], "dos réplicas de 3 fotogramas: una frontera, en el 3"
    p3 = payload(per_frame_dg_kcal=[-30.0] * 9,
                 replica_detail=[{"replica": i, "n_frames": 3} for i in range(3)])
    assert one(p3, "converge")[0]["boundaries"] == [3, 6]


def test_una_sola_replica_no_inventa_fronteras():
    p = payload(replica_detail=[{"replica": 0, "n_frames": 6}], n_replicas_completed=1)
    assert one(p, "converge")[0]["boundaries"] == []
    assert not [b for b in blocks(p) if b.get("type") == "text" and "replicas independientes" in b.get("text", "")]


def test_se_dice_cuando_son_replicas_y_no_una_trayectoria():
    txt = " ".join(b.get("text", "") for b in blocks(payload()) if b.get("type") == "text")
    assert "replicas independientes" in txt and "no una trayectoria seguida" in txt


# ------------------------------------------------------------------ lo que NO se rellena
def test_un_fotograma_sin_numero_se_cae_y_se_nombra():
    p = payload(per_frame_dg_kcal=[-31.2, None, -30.5, float("nan"), -30.9])
    b = one(p, "converge")[0]
    assert [x[1] for x in b["points"]] == [-31.2, -30.5, -30.9]
    why = " ".join(a["why"] for a in one(p, "absent"))
    assert "2 de 5" in why and "nadie tomo" in why


def test_menos_de_dos_fotogramas_utiles_no_es_un_grafico():
    p = payload(per_frame_dg_kcal=[-31.2, None, float("nan")])
    assert one(p, "converge") == []
    assert any("convergencia" in a["why"] for a in one(p, "absent"))


def test_una_corrida_sin_fotogramas_no_dibuja_nada():
    p = {"available": False, "reason": "no production frames produced a valid MM-GBSA score",
         "method": "short-MD-trajectory MM-GBSA (OpenMM)"}
    assert one(p, "converge") == []


def test_una_parada_temprana_se_dice_con_su_motivo():
    p = payload(stopped_early=True, stop_reason="wall-clock budget exhausted after replica 1")
    why = " ".join(a["why"] for a in one(p, "absent"))
    assert "wall-clock budget exhausted" in why


# ------------------------------------------------------------------ el descargo no se entierra
def test_el_descargo_del_motor_viaja_visible():
    txt = " ".join(b.get("text", "") for b in blocks(payload()) if b.get("type") == "text")
    assert "NOT a converged free-energy result" in txt
    assert "lo que este numero NO es" in txt, "el descargo llegó sin su etiqueta"


# ------------------------------------------------------------------ lo que hace la ventana
def test_la_ventana_dibuja_la_media_corriente_y_las_fronteras():
    assert "function convergeNode(" in JS
    assert 'b.type === "converge"' in JS, "el bloque no está en el despacho"
    assert "ch-split" in JS, "las fronteras entre réplicas no se dibujan"
    assert "media corriente" in JS, "la ventana no dice qué es la línea"


def test_la_ventana_no_llama_tiempo_al_eje_de_fotogramas():
    """El eje ES el fotograma. Si alguien lo reetiqueta como ps, esto cae: con varias réplicas, el tiempo
    acumulado no existe."""
    i = JS.index("function convergeNode(")
    cuerpo = JS[i:i + 4200]
    assert '(b.x_label || "fotograma")' in cuerpo
    assert "1 fotograma cada " in cuerpo, "el intervalo se dice por fotograma, no como eje"


def test_la_banda_es_media_mas_menos_desviacion_y_no_un_intervalo_de_confianza():
    i = JS.index("function convergeNode(")
    cuerpo = JS[i:i + 4200]
    assert "media ${fmtNum(b.mean)} ± ${fmtNum(b.std)}" in cuerpo
    assert "confianza" not in cuerpo, "una desviación no es un intervalo de confianza"
