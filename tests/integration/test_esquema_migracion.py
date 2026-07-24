import sqlite3
from pathlib import Path

import pytest

MIGRACION = Path(__file__).parents[2] / "bot" / "db" / "migraciones" / "001_esquema_inicial.sql"


@pytest.fixture
def conn():
    con = sqlite3.connect(":memory:")
    con.executescript(MIGRACION.read_text())
    yield con
    con.close()


def test_migracion_crea_las_tablas_esperadas(conn):
    tablas = {
        fila[0]
        for fila in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {"usuarios", "saldos", "tickets", "selecciones", "movimientos"} <= tablas


def test_ux_mov_pago_unico_rechaza_segundo_payout(conn):
    ts = 1_700_000_000
    conn.execute(
        "INSERT INTO usuarios (id, username, creado_ts) VALUES (1, 'demo', ?)",
        (ts,),
    )
    conn.execute(
        "INSERT INTO tickets (id, usuario_id, moneda, stake_cent, cuota_milesimas, "
        "payout_pot_cent, estado, creado_ts) "
        "VALUES (1, 1, 'CUP', 50000, 1500, 75000, 'ganado', ?)",
        (ts,),
    )
    conn.execute(
        "INSERT INTO movimientos (usuario_id, moneda, centavos, tipo, ticket_id, ts) "
        "VALUES (1, 'CUP', 75000, 'payout', 1, ?)",
        (ts,),
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO movimientos (usuario_id, moneda, centavos, tipo, ticket_id, ts) "
            "VALUES (1, 'CUP', 75000, 'payout', 1, ?)",
            (ts,),
        )


def test_ux_mov_pago_unico_permite_payout_y_devolucion_distintos(conn):
    """El índice es (ticket_id, tipo): un payout en un ticket y una
    devolución en otro no deben chocar entre sí."""
    ts = 1_700_000_000
    conn.execute(
        "INSERT INTO usuarios (id, username, creado_ts) VALUES (1, 'demo', ?)",
        (ts,),
    )
    for ticket_id, estado in ((1, "ganado"), (2, "nulo")):
        conn.execute(
            "INSERT INTO tickets (id, usuario_id, moneda, stake_cent, cuota_milesimas, "
            "payout_pot_cent, estado, creado_ts) "
            "VALUES (?, 1, 'CUP', 50000, 1500, 75000, ?, ?)",
            (ticket_id, estado, ts),
        )

    conn.execute(
        "INSERT INTO movimientos (usuario_id, moneda, centavos, tipo, ticket_id, ts) "
        "VALUES (1, 'CUP', 75000, 'payout', 1, ?)",
        (ts,),
    )
    conn.execute(
        "INSERT INTO movimientos (usuario_id, moneda, centavos, tipo, ticket_id, ts) "
        "VALUES (1, 'CUP', 50000, 'devolucion', 2, ?)",
        (ts,),
    )
    conn.commit()
