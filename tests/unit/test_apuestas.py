import sqlite3
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from bot.core import apuestas, exposicion
from bot.core.apuestas import EstadoAceptacion, SeleccionEntrada
from bot.fuente_resultados.cache_cuotas import CuotaVigente

MIGRACION_001 = Path(__file__).parents[2] / "bot" / "db" / "migraciones" / "001_esquema_inicial.sql"

CREADO_TS = 1_700_000_000
AHORA_TS = 1_700_050_000
INICIO_PARTIDO_TS = AHORA_TS + 3600


@pytest.fixture
def conn():
    con = sqlite3.connect(":memory:")
    con.executescript(MIGRACION_001.read_text())
    yield con
    con.close()


def _crear_usuario(conn, usuario_id=1, moneda="CUP", saldo_cent=0):
    conn.execute(
        "INSERT INTO usuarios (id, username, creado_ts) VALUES (?, 'demo', ?)",
        (usuario_id, CREADO_TS),
    )
    conn.execute(
        "INSERT INTO saldos (usuario_id, moneda, centavos) VALUES (?, ?, ?)",
        (usuario_id, moneda, saldo_cent),
    )
    conn.commit()


def _crear_ticket_pendiente(conn, ticket_id, usuario_id, moneda, payout_pot_cent, game_ids):
    conn.execute(
        "INSERT INTO tickets (id, usuario_id, moneda, stake_cent, cuota_milesimas, "
        "payout_pot_cent, estado, creado_ts) VALUES (?, ?, ?, 100, 1500, ?, 'pendiente', ?)",
        (ticket_id, usuario_id, moneda, payout_pot_cent, CREADO_TS),
    )
    for game_id in game_ids:
        conn.execute(
            "INSERT INTO selecciones (ticket_id, game_id, champ_id, sport_id, market_type, "
            "cuota_milesimas, equipo_local, equipo_visitante, inicio_ts, estado) "
            "VALUES (?, ?, 1, 1, 1, 1500, 'Local', 'Visitante', ?, 'pendiente')",
            (ticket_id, game_id, CREADO_TS),
        )
    conn.commit()


def _seleccion(game_id, market_type=1, parametro=None, cuota_vista_ms=2000, champ_id=1):
    return SeleccionEntrada(
        game_id=game_id,
        champ_id=champ_id,
        sport_id=1,
        market_type=market_type,
        parametro=parametro,
        cuota_vista_milesimas=cuota_vista_ms,
        inicio_ts=INICIO_PARTIDO_TS,
        equipo_local="Local",
        equipo_visitante="Visitante",
    )


def _mock_cache(vigentes: dict):
    """vigentes: {(game_id, market_type, parametro): cuota_milesimas | None}."""

    def _obtener(game_id, market_type, parametro, ahora_ts):
        clave = (game_id, market_type, parametro)
        if clave not in vigentes or vigentes[clave] is None:
            return None
        return CuotaVigente(
            game_id=game_id,
            champ_id=1,
            market_type=market_type,
            parametro=parametro,
            cuota_milesimas=vigentes[clave],
            capturada_ts=ahora_ts,
        )

    return _obtener


def _sin_filas(conn, tabla):
    return conn.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0] == 0


# --- 1. Simple aceptada -------------------------------------------------


def test_simple_aceptada_escribe_todo_consistente(conn):
    _crear_usuario(conn, saldo_cent=1000_00)
    seleccion = _seleccion(game_id=100, cuota_vista_ms=2000)

    with patch.object(apuestas, "obtener_cuota_fresca", _mock_cache({(100, 1, None): 2000})):
        resultado = apuestas.aceptar_apuesta(conn, 1, "CUP", 100_00, [seleccion], AHORA_TS)

    assert resultado.estado == EstadoAceptacion.ACEPTADA
    ticket_id = resultado.ticket_id

    ticket = conn.execute(
        "SELECT estado, cuota_milesimas, payout_pot_cent, stake_cent FROM tickets WHERE id = ?", (ticket_id,)
    ).fetchone()
    assert ticket == ("pendiente", 2000, 200_00, 100_00)

    seleccion_db = conn.execute(
        "SELECT game_id, market_type, cuota_milesimas, estado FROM selecciones WHERE ticket_id = ?", (ticket_id,)
    ).fetchone()
    assert seleccion_db == (100, 1, 2000, "pendiente")

    movimiento = conn.execute(
        "SELECT centavos, tipo, ticket_id FROM movimientos WHERE usuario_id = 1"
    ).fetchone()
    assert movimiento == (-100_00, "stake", ticket_id)

    saldo = conn.execute("SELECT centavos FROM saldos WHERE usuario_id = 1 AND moneda = 'CUP'").fetchone()[0]
    assert saldo == 1000_00 - 100_00


# --- 2. Saldo insuficiente ------------------------------------------------


def test_saldo_insuficiente_no_escribe_nada(conn):
    _crear_usuario(conn, saldo_cent=0)
    seleccion = _seleccion(game_id=100, cuota_vista_ms=2000)

    with patch.object(apuestas, "obtener_cuota_fresca", _mock_cache({(100, 1, None): 2000})):
        resultado = apuestas.aceptar_apuesta(conn, 1, "CUP", 20_00, [seleccion], AHORA_TS)

    assert resultado.estado == EstadoAceptacion.RECHAZADA
    assert "saldo" in resultado.motivo
    assert _sin_filas(conn, "tickets")
    assert _sin_filas(conn, "movimientos")
    saldo = conn.execute("SELECT centavos FROM saldos WHERE usuario_id = 1 AND moneda = 'CUP'").fetchone()[0]
    assert saldo == 0


# --- 3. Combinada válida ---------------------------------------------------


def test_combinada_de_3_valida_payout_es_producto_de_cuotas(conn):
    _crear_usuario(conn, saldo_cent=1000_00)
    selecciones = [
        _seleccion(game_id=10, cuota_vista_ms=1500),
        _seleccion(game_id=20, cuota_vista_ms=1500),
        _seleccion(game_id=30, cuota_vista_ms=1500),
    ]
    cache = _mock_cache({(10, 1, None): 1500, (20, 1, None): 1500, (30, 1, None): 1500})

    with patch.object(apuestas, "obtener_cuota_fresca", cache):
        resultado = apuestas.aceptar_apuesta(conn, 1, "CUP", 20_00, selecciones, AHORA_TS)

    assert resultado.estado == EstadoAceptacion.ACEPTADA

    tickets = conn.execute("SELECT cuota_milesimas, payout_pot_cent FROM tickets").fetchall()
    assert len(tickets) == 1
    cuota_ms, payout_cent = tickets[0]
    assert cuota_ms == 3375  # 1.5 * 1.5 * 1.5 = 3.375
    assert payout_cent == apuestas.payout_combinada_cent(20_00, 3375)

    selecciones_db = conn.execute("SELECT game_id FROM selecciones").fetchall()
    assert {row[0] for row in selecciones_db} == {10, 20, 30}


# --- 4. Combinada con 2 patas del mismo game_id ---------------------------


def test_combinada_mismo_game_id_rechazada_sin_tocar_la_cache(conn):
    _crear_usuario(conn, saldo_cent=1000_00)
    selecciones = [
        _seleccion(game_id=1, market_type=1, cuota_vista_ms=1500),
        _seleccion(game_id=1, market_type=9, parametro=Decimal("2.5"), cuota_vista_ms=1500),
    ]

    def _cache_no_deberia_llamarse(*args, **kwargs):
        raise AssertionError("combinada_valida debe cortar antes de tocar la cache")

    with patch.object(apuestas, "obtener_cuota_fresca", _cache_no_deberia_llamarse):
        resultado = apuestas.aceptar_apuesta(conn, 1, "CUP", 20_00, selecciones, AHORA_TS)

    assert resultado.estado == EstadoAceptacion.RECHAZADA
    assert _sin_filas(conn, "tickets")


# --- 5. Una pata sin cuota fresca ------------------------------------------


def test_pata_sin_cuota_fresca_rechaza_toda_la_apuesta(conn):
    _crear_usuario(conn, saldo_cent=1000_00)
    selecciones = [
        _seleccion(game_id=10, cuota_vista_ms=1500),
        _seleccion(game_id=20, cuota_vista_ms=1500),
    ]
    cache = _mock_cache({(10, 1, None): 1500})  # (20, 1, None) ausente

    with patch.object(apuestas, "obtener_cuota_fresca", cache):
        resultado = apuestas.aceptar_apuesta(conn, 1, "CUP", 20_00, selecciones, AHORA_TS)

    assert resultado.estado == EstadoAceptacion.RECHAZADA
    assert _sin_filas(conn, "tickets")
    assert _sin_filas(conn, "movimientos")


# --- 6. Baja de cuota > 2% ------------------------------------------------


def test_baja_mayor_a_umbral_devuelve_reconfirmar_sin_escribir(conn):
    _crear_usuario(conn, saldo_cent=1000_00)
    seleccion = _seleccion(game_id=100, cuota_vista_ms=2000)
    cache = _mock_cache({(100, 1, None): 1900})  # baja de 5%

    with patch.object(apuestas, "obtener_cuota_fresca", cache):
        resultado = apuestas.aceptar_apuesta(conn, 1, "CUP", 20_00, [seleccion], AHORA_TS)

    assert resultado.estado == EstadoAceptacion.RECONFIRMAR
    assert resultado.cuotas_para_reconfirmar[0].cuota_vigente_milesimas == 1900
    assert resultado.cuotas_para_reconfirmar[0].cuota_vista_milesimas == 2000
    assert _sin_filas(conn, "tickets")
    saldo = conn.execute("SELECT centavos FROM saldos WHERE usuario_id = 1 AND moneda = 'CUP'").fetchone()[0]
    assert saldo == 1000_00


# --- 7. Baja <= 2% se sella a la vigente (peor) ---------------------------


def test_baja_dentro_del_umbral_sella_a_la_vigente(conn):
    _crear_usuario(conn, saldo_cent=1000_00)
    seleccion = _seleccion(game_id=100, cuota_vista_ms=2000)
    cache = _mock_cache({(100, 1, None): 1970})  # baja de 1.5%

    with patch.object(apuestas, "obtener_cuota_fresca", cache):
        resultado = apuestas.aceptar_apuesta(conn, 1, "CUP", 20_00, [seleccion], AHORA_TS)

    assert resultado.estado == EstadoAceptacion.ACEPTADA
    cuota_sellada = conn.execute("SELECT cuota_milesimas FROM selecciones").fetchone()[0]
    assert cuota_sellada == 1970


# --- 8. Cuota mejoró se sella a la vista -----------------------------------


def test_cuota_mejorada_sella_a_la_vista(conn):
    _crear_usuario(conn, saldo_cent=1000_00)
    seleccion = _seleccion(game_id=100, cuota_vista_ms=2000)
    cache = _mock_cache({(100, 1, None): 2100})  # mejoró

    with patch.object(apuestas, "obtener_cuota_fresca", cache):
        resultado = apuestas.aceptar_apuesta(conn, 1, "CUP", 20_00, [seleccion], AHORA_TS)

    assert resultado.estado == EstadoAceptacion.ACEPTADA
    cuota_sellada = conn.execute("SELECT cuota_milesimas FROM selecciones").fetchone()[0]
    assert cuota_sellada == 2000


# --- 9. payout > PAYOUT_MAX_TICKET ------------------------------------------


def test_payout_supera_el_maximo_es_rechazada(conn):
    _crear_usuario(conn, saldo_cent=1000_00_00)
    seleccion = _seleccion(game_id=100, cuota_vista_ms=10_000)  # cuota 10.0
    cache = _mock_cache({(100, 1, None): 10_000})
    stake_cent = apuestas.STAKE_MAX_SIMPLE["CUP"]  # payout = stake * 10 >> PAYOUT_MAX_TICKET

    with patch.object(apuestas, "obtener_cuota_fresca", cache):
        resultado = apuestas.aceptar_apuesta(conn, 1, "CUP", stake_cent, [seleccion], AHORA_TS)

    assert resultado.estado == EstadoAceptacion.RECHAZADA
    assert "PAYOUT_MAX_TICKET" in resultado.motivo
    assert _sin_filas(conn, "tickets")


# --- 10. Exposición de evento excedida -------------------------------------


def test_exposicion_de_evento_excedida_rechaza_y_no_toca_saldo(conn):
    _crear_usuario(conn, saldo_cent=1000_00)
    limite_evento = exposicion.EXPOSICION_MAX_EVENTO["CUP"]
    _crear_ticket_pendiente(conn, 1, 1, "CUP", limite_evento, [500])

    seleccion = _seleccion(game_id=500, cuota_vista_ms=2000)
    cache = _mock_cache({(500, 1, None): 2000})

    with patch.object(apuestas, "obtener_cuota_fresca", cache):
        resultado = apuestas.aceptar_apuesta(conn, 1, "CUP", 20_00, [seleccion], AHORA_TS)

    assert resultado.estado == EstadoAceptacion.RECHAZADA
    assert "EXPOSICION_MAX_EVENTO" in resultado.motivo
    tickets = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    assert tickets == 1  # solo el que ya existía
    saldo = conn.execute("SELECT centavos FROM saldos WHERE usuario_id = 1 AND moneda = 'CUP'").fetchone()[0]
    assert saldo == 1000_00


# --- 11. Atomicidad: dos aceptaciones que juntas exceden el límite --------


def test_atomicidad_segunda_aceptacion_falla_si_juntas_exceden_el_evento(conn):
    _crear_usuario(conn, saldo_cent=10_000_00)
    limite = exposicion.EXPOSICION_MAX_EVENTO["CUP"]

    # Un ticket ya existente deja poco margen. PAYOUT_MAX_TICKET (5% de
    # banca) es la mitad de EXPOSICION_MAX_EVENTO (10%): un solo ticket
    # nunca puede exceder el evento por sí solo, así que para probar que la
    # SEGUNDA llamada ve el commit de la primera hace falta un tercer
    # ticket previo, no dos aceptaciones "al límite" nada más.
    _crear_ticket_pendiente(conn, 1, 1, "CUP", 150_000, [900])

    seleccion = _seleccion(game_id=900, cuota_vista_ms=10_000)  # cuota 10.0
    cache = _mock_cache({(900, 1, None): 10_000})
    stake_cent = 200_00  # payout = 200_00 * 10 = 2_000_00 (200000 centavos)

    with patch.object(apuestas, "obtener_cuota_fresca", cache):
        primero = apuestas.aceptar_apuesta(conn, 1, "CUP", stake_cent, [seleccion], AHORA_TS)
        segundo = apuestas.aceptar_apuesta(conn, 1, "CUP", stake_cent, [seleccion], AHORA_TS)

    # 150_000 (previo) + 200_000 (primero) = 350_000 <= limite: entra.
    # 350_000 + 200_000 = 550_000 > limite: la segunda debe fallar, y solo
    # falla si recalculó la exposición viendo el ticket que la primera
    # llamada ya comiteó.
    assert primero.estado == EstadoAceptacion.ACEPTADA
    assert segundo.estado == EstadoAceptacion.RECHAZADA
    assert "EXPOSICION_MAX_EVENTO" in segundo.motivo
    assert limite == 500_000  # supuesto de este test sobre BANCAS/LIMITES.md actuales

    tickets = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    assert tickets == 2  # el previo + el primero aceptado


# --- parametro negativo: se guarda y se recupera bien (hándicap -1.5) -----


def test_parametro_negativo_se_guarda_como_linea_no_como_dinero(conn):
    """a_centavos (dinero) coincide numéricamente con linea_a_centesimas
    (línea) para -1.5, pero son conversiones distintas por contrato: esto
    prueba el camino real de aceptar_apuesta(), no solo la función pura."""
    _crear_usuario(conn, saldo_cent=1000_00)
    seleccion = _seleccion(game_id=100, market_type=7, parametro=Decimal("-1.5"), cuota_vista_ms=1900)
    cache = _mock_cache({(100, 7, Decimal("-1.5")): 1900})

    with patch.object(apuestas, "obtener_cuota_fresca", cache):
        resultado = apuestas.aceptar_apuesta(conn, 1, "CUP", 20_00, [seleccion], AHORA_TS)

    assert resultado.estado == EstadoAceptacion.ACEPTADA
    parametro_centesimas = conn.execute("SELECT parametro_centesimas FROM selecciones").fetchone()[0]
    assert parametro_centesimas == -150


# --- 12. El ledger cuadra tras un lote --------------------------------------


def test_ledger_cuadra_tras_aceptar_un_lote(conn):
    _crear_usuario(conn, usuario_id=1, saldo_cent=1000_00)
    _crear_usuario(conn, usuario_id=2, saldo_cent=500_00)

    cache = _mock_cache(
        {
            (100, 1, None): 2000,
            (200, 1, None): 1500,
            (300, 1, None): 3000,
        }
    )

    with patch.object(apuestas, "obtener_cuota_fresca", cache):
        r1 = apuestas.aceptar_apuesta(conn, 1, "CUP", 50_00, [_seleccion(100, cuota_vista_ms=2000)], AHORA_TS)
        r2 = apuestas.aceptar_apuesta(conn, 1, "CUP", 30_00, [_seleccion(200, cuota_vista_ms=1500)], AHORA_TS)
        r3 = apuestas.aceptar_apuesta(conn, 2, "CUP", 40_00, [_seleccion(300, cuota_vista_ms=3000)], AHORA_TS)

    assert r1.estado == r2.estado == r3.estado == EstadoAceptacion.ACEPTADA

    for usuario_id, saldo_inicial in ((1, 1000_00), (2, 500_00)):
        movimientos_total = conn.execute(
            "SELECT COALESCE(SUM(centavos), 0) FROM movimientos WHERE usuario_id = ? AND moneda = 'CUP'",
            (usuario_id,),
        ).fetchone()[0]
        saldo_final = conn.execute(
            "SELECT centavos FROM saldos WHERE usuario_id = ? AND moneda = 'CUP'", (usuario_id,)
        ).fetchone()[0]
        assert saldo_final == saldo_inicial + movimientos_total
