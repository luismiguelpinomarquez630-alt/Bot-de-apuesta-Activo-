import asyncio
import sqlite3
import time
from unittest.mock import patch

import bot.__main__ as bot_main
from bot.db.migrar import aplicar_migraciones
from bot.fuente_resultados import cache_cuotas
from bot.fuente_resultados.primaria import cliente_1x


def _run(coro):
    return asyncio.run(coro)


# --- construir_scheduler: los jobs quedan registrados con la config correcta


def test_scheduler_registra_los_dos_jobs_con_max_instances_y_coalesce():
    config = bot_main.Config(
        db_path=":memory:",
        sport_ids=[1],
        intervalo_refresco_s=15,
        intervalo_liquidacion_s=180,
        count_cuotas=1000,
    )

    scheduler = bot_main.construir_scheduler(config)
    jobs = {job.id: job for job in scheduler.get_jobs()}

    assert set(jobs) == {"refrescar_cuotas", "liquidar_pendientes"}
    for job in jobs.values():
        assert job.max_instances == 1
        assert job.coalesce is True

    assert jobs["refrescar_cuotas"].trigger.interval.total_seconds() == 15
    assert jobs["liquidar_pendientes"].trigger.interval.total_seconds() == 180


# --- arranque + apagado: migra, y el apagado cierra el cliente httpx -------


def test_arranque_migra_y_apagado_cierra_cliente(tmp_path):
    db_path = str(tmp_path / "arranque.db")
    config = bot_main.Config(
        db_path=db_path,
        sport_ids=[1],
        intervalo_refresco_s=3600,  # que no dispare durante el test
        intervalo_liquidacion_s=3600,
        count_cuotas=1000,
    )
    detener = asyncio.Event()
    detener.set()  # ya "señalado": main_async debe pasar de largo a apagar

    with patch.object(cliente_1x, "cerrar_cliente") as mock_cerrar:
        _run(bot_main.main_async(config, detener=detener))

    mock_cerrar.assert_called_once()

    # La migración corrió y la conexión de arranque se cerró: una conexión
    # nueva al mismo archivo ve el esquema completo sin bloqueos.
    conn = sqlite3.connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 2
    tablas = {fila[0] for fila in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"tickets", "selecciones", "observaciones_resultado"} <= tablas
    conn.close()


# --- ciclo completo: refresco puebla la caché, liquidación paga -----------


def test_ciclo_completo_refresco_y_liquidacion(tmp_path):
    """No hay Telegram todavía (bot/telegram/ sigue vacío): el ticket de
    prueba NO entra por aceptar_apuesta, se inserta directo como fixture de
    DB. Este test cubre _job_refrescar_cuotas y _job_liquidar_pendientes de
    punta a punta, con cliente_1x mockeado."""
    db_path = str(tmp_path / "ciclo.db")
    game_id = 555001

    conn = bot_main._abrir_conexion(db_path)
    aplicar_migraciones(conn)

    ahora = time.time()
    inicio_ts = int(ahora - 3 * 3600)  # el partido arrancó hace 3h
    conn.execute("INSERT INTO usuarios (id, username, creado_ts) VALUES (1, 'demo', ?)", (int(ahora),))
    conn.execute("INSERT INTO saldos (usuario_id, moneda, centavos) VALUES (1, 'CUP', 0)")
    conn.execute(
        "INSERT INTO tickets (id, usuario_id, moneda, stake_cent, cuota_milesimas, "
        "payout_pot_cent, estado, creado_ts) VALUES (1, 1, 'CUP', 1000, 2000, 2000, 'pendiente', ?)",
        (int(ahora),),
    )
    conn.execute(
        "INSERT INTO selecciones (ticket_id, game_id, champ_id, sport_id, market_type, "
        "cuota_milesimas, equipo_local, equipo_visitante, inicio_ts, estado) "
        "VALUES (1, ?, 1, 1, 1, 2000, 'Local', 'Visitante', ?, 'pendiente')",
        (game_id, inicio_ts),
    )
    # Observación previa ya estable: primera vez vista hace ~16 min
    # (satisface los 15 min de estabilidad), pero re-chequeada "ahora"
    # (después de ARRANQUE_TS de settlement_engine, que se capturó al
    # importar el módulo, antes que esta línea) para no caer en la guarda
    # de arranque.
    # +5s de margen sobre ARRANQUE_TS: settlement_engine se importa (y
    # captura su ARRANQUE_TS) al arrancar el proceso de test, que puede
    # caer en el mismo segundo entero que "ahora" en una suite rápida.
    conn.execute(
        "INSERT INTO observaciones_resultado (game_id, marcador_raw, visto_primera_vez_ts, ultima_consulta_ts) "
        "VALUES (?, '2:0 (1:0,1:0)', ?, ?)",
        (game_id, int(ahora - 1000), int(ahora) + 5),
    )
    conn.commit()
    conn.close()

    evento = cliente_1x.EventoCuotas(
        game_id=game_id,
        champ_id=1,
        sport_id=1,
        inicio_ts=inicio_ts,
        equipo_local="Local",
        equipo_visitante="Visitante",
        mercados=[cliente_1x.MercadoCuota(tipo=1, cuota_milesimas=2000, parametro=None, group=1)],
    )
    liga = cliente_1x.LigaConCuotas(champ_id=1)
    with patch.object(cliente_1x, "obtener_ligas_con_cuotas", return_value=[liga]), patch.object(
        cliente_1x, "obtener_cuotas", return_value=[evento]
    ):
        _run(bot_main._job_refrescar_cuotas([1], 1000))

    cuota_fresca = cache_cuotas.obtener_cuota_fresca(game_id, 1, None, int(time.time()))
    assert cuota_fresca is not None
    assert cuota_fresca.cuota_milesimas == 2000

    item_partido = {
        "id": game_id,
        "sportId": 1,
        "opp1Ids": [111],
        "opp2Ids": [222],
        "dopInfo": None,
        "score": "2:0 (1:0,1:0)",
    }
    with patch.object(cliente_1x, "obtener_partidos", return_value=[item_partido]):
        _run(bot_main._job_liquidar_pendientes(db_path))

    conn = sqlite3.connect(db_path)
    estado_ticket = conn.execute("SELECT estado FROM tickets WHERE id = 1").fetchone()[0]
    assert estado_ticket == "ganado"
    movimiento = conn.execute("SELECT tipo, centavos FROM movimientos WHERE ticket_id = 1").fetchone()
    assert movimiento == ("payout", 2000)
    conn.close()
