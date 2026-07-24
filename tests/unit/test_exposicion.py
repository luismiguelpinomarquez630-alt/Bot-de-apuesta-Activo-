import sqlite3
from pathlib import Path

import pytest

from bot.core import exposicion

MIGRACION_001 = Path(__file__).parents[2] / "bot" / "db" / "migraciones" / "001_esquema_inicial.sql"

CREADO_TS = 1_700_000_000
AHORA_TS = 1_700_050_000


@pytest.fixture
def conn():
    con = sqlite3.connect(":memory:")
    con.executescript(MIGRACION_001.read_text())
    yield con
    con.close()


def _crear_usuario(conn, usuario_id=1):
    conn.execute(
        "INSERT INTO usuarios (id, username, creado_ts) VALUES (?, 'demo', ?)",
        (usuario_id, CREADO_TS),
    )
    conn.commit()


def _crear_ticket(conn, ticket_id, usuario_id, moneda, payout_pot_cent, game_ids, estado="pendiente"):
    """game_ids: lista de game_id, una selección pendiente por cada uno."""
    conn.execute(
        "INSERT INTO tickets (id, usuario_id, moneda, stake_cent, cuota_milesimas, "
        "payout_pot_cent, estado, creado_ts) VALUES (?, ?, ?, 100, 1500, ?, ?, ?)",
        (ticket_id, usuario_id, moneda, payout_pot_cent, estado, CREADO_TS),
    )
    for game_id in game_ids:
        conn.execute(
            "INSERT INTO selecciones (ticket_id, game_id, champ_id, sport_id, market_type, "
            "cuota_milesimas, equipo_local, equipo_visitante, inicio_ts, estado) "
            "VALUES (?, ?, 1, 1, 1, 1500, 'Local', 'Visitante', ?, 'pendiente')",
            (ticket_id, game_id, CREADO_TS),
        )
    conn.commit()


# --- exposicion_evento -------------------------------------------------------


def test_evento_sin_apuestas_expone_cero(conn):
    assert exposicion.exposicion_evento(conn, game_id=1, moneda="CUP") == 0


def test_tres_tickets_sobre_un_game_id_suman(conn):
    _crear_usuario(conn)
    _crear_ticket(conn, 1, 1, "CUP", 100_00, [500])
    _crear_ticket(conn, 2, 1, "CUP", 200_00, [500])
    _crear_ticket(conn, 3, 1, "CUP", 300_00, [500])

    assert exposicion.exposicion_evento(conn, game_id=500, moneda="CUP") == 600_00


def test_combinada_cuenta_entero_en_cada_uno_de_sus_3_eventos(conn):
    _crear_usuario(conn)
    _crear_ticket(conn, 1, 1, "CUP", 400_00, [10, 20, 30])

    assert exposicion.exposicion_evento(conn, game_id=10, moneda="CUP") == 400_00
    assert exposicion.exposicion_evento(conn, game_id=20, moneda="CUP") == 400_00
    assert exposicion.exposicion_evento(conn, game_id=30, moneda="CUP") == 400_00


def test_ticket_con_dos_patas_del_mismo_game_id_cuenta_una_vez(conn):
    _crear_usuario(conn)
    conn.execute(
        "INSERT INTO tickets (id, usuario_id, moneda, stake_cent, cuota_milesimas, "
        "payout_pot_cent, estado, creado_ts) VALUES (1, 1, 'CUP', 100, 4000, ?, 'pendiente', ?)",
        (500_00, CREADO_TS),
    )
    for market_type in (1, 9):
        conn.execute(
            "INSERT INTO selecciones (ticket_id, game_id, champ_id, sport_id, market_type, "
            "cuota_milesimas, equipo_local, equipo_visitante, inicio_ts, estado) "
            "VALUES (1, 777, 1, 1, ?, 2000, 'Local', 'Visitante', ?, 'pendiente')",
            (market_type, CREADO_TS),
        )
    conn.commit()

    assert exposicion.exposicion_evento(conn, game_id=777, moneda="CUP") == 500_00


def test_ticket_resuelto_no_cuenta(conn):
    _crear_usuario(conn)
    _crear_ticket(conn, 1, 1, "CUP", 100_00, [500], estado="ganado")
    _crear_ticket(conn, 2, 1, "CUP", 200_00, [500], estado="perdido")
    _crear_ticket(conn, 3, 1, "CUP", 300_00, [500], estado="pendiente")

    assert exposicion.exposicion_evento(conn, game_id=500, moneda="CUP") == 300_00


# --- exposicion_global --------------------------------------------------------


def test_exposicion_global_suma_todos_los_pendientes_de_la_moneda(conn):
    _crear_usuario(conn)
    _crear_ticket(conn, 1, 1, "CUP", 100_00, [1])
    _crear_ticket(conn, 2, 1, "CUP", 200_00, [2])
    _crear_ticket(conn, 3, 1, "CUP", 50_00, [3], estado="ganado")

    assert exposicion.exposicion_global(conn, moneda="CUP") == 300_00


# --- cabe_apuesta --------------------------------------------------------------


def test_cabe_apuesta_rechaza_si_supera_el_evento(conn):
    _crear_usuario(conn)
    limite = exposicion.EXPOSICION_MAX_EVENTO["CUP"]
    _crear_ticket(conn, 1, 1, "CUP", limite, [500])

    resultado = exposicion.cabe_apuesta(conn, "CUP", game_ids=[500], payout_nuevo_cent=1, ahora_ts=AHORA_TS)

    assert resultado.cabe is False
    assert resultado.limite_excedido == "EXPOSICION_MAX_EVENTO"


def test_cabe_apuesta_rechaza_si_supera_el_global(conn):
    _crear_usuario(conn)
    limite_evento = exposicion.EXPOSICION_MAX_EVENTO["CUP"]
    limite_global = exposicion.EXPOSICION_MAX_GLOBAL["CUP"]
    # Varios eventos, cada uno bajo su propio tope, pero que entre todos
    # llenan el global.
    restante = limite_global
    game_id = 1000
    ticket_id = 1
    while restante > 0:
        monto = min(limite_evento, restante)
        _crear_ticket(conn, ticket_id, 1, "CUP", monto, [game_id])
        restante -= monto
        game_id += 1
        ticket_id += 1

    resultado = exposicion.cabe_apuesta(
        conn, "CUP", game_ids=[game_id], payout_nuevo_cent=1, ahora_ts=AHORA_TS
    )

    assert resultado.cabe is False
    assert resultado.limite_excedido == "EXPOSICION_MAX_GLOBAL"


def test_cabe_apuesta_usd_usa_la_banca_de_usd_no_cup(conn):
    _crear_usuario(conn)
    # Llenamos CUP hasta el tope de su evento — no debe afectar el chequeo en USD.
    _crear_ticket(conn, 1, 1, "CUP", exposicion.EXPOSICION_MAX_EVENTO["CUP"], [500])

    resultado = exposicion.cabe_apuesta(
        conn, "USD", game_ids=[500], payout_nuevo_cent=1_00, ahora_ts=AHORA_TS
    )

    assert resultado.cabe is True


def test_cabe_apuesta_acepta_dentro_de_los_limites(conn):
    _crear_usuario(conn)
    resultado = exposicion.cabe_apuesta(conn, "CUP", game_ids=[1, 2], payout_nuevo_cent=100_00, ahora_ts=AHORA_TS)

    assert resultado.cabe is True
    assert resultado.limite_excedido is None
