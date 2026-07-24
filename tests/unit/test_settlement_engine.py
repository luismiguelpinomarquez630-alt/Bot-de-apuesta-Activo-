import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from bot.core import settlement_engine
from bot.core.liquidacion_tickets import EstadoTicket
from bot.fuente_resultados import cascada_fuentes
from bot.fuente_resultados.cascada_fuentes import EstadoResultado, ResultadoEvaluado

MIGRACION_001 = Path(__file__).parents[2] / "bot" / "db" / "migraciones" / "001_esquema_inicial.sql"
MIGRACION_002 = Path(__file__).parents[2] / "bot" / "db" / "migraciones" / "002_observaciones_resultado.sql"

CREADO_TS = 1_700_000_000
AHORA_TS = 1_700_050_000  # bastante después: no interfiere con guardas de tiempo


@pytest.fixture
def conn():
    con = sqlite3.connect(":memory:")
    con.executescript(MIGRACION_001.read_text())
    con.executescript(MIGRACION_002.read_text())
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


def _crear_ticket(conn, ticket_id, usuario_id, moneda, stake_cent, selecciones):
    """selecciones: lista de dicts con game_id, champ_id, market_type,
    parametro_centesimas, cuota_milesimas, inicio_ts."""
    conn.execute(
        "INSERT INTO tickets (id, usuario_id, moneda, stake_cent, cuota_milesimas, "
        "payout_pot_cent, estado, creado_ts) VALUES (?, ?, ?, ?, 1500, 1, 'pendiente', ?)",
        (ticket_id, usuario_id, moneda, stake_cent, CREADO_TS),
    )
    for sel in selecciones:
        conn.execute(
            "INSERT INTO selecciones (ticket_id, game_id, champ_id, sport_id, market_type, "
            "parametro_centesimas, cuota_milesimas, equipo_local, equipo_visitante, inicio_ts, estado) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, 'Local', 'Visitante', ?, 'pendiente')",
            (
                ticket_id,
                sel["game_id"],
                sel["champ_id"],
                sel["market_type"],
                sel.get("parametro_centesimas"),
                sel["cuota_milesimas"],
                sel["inicio_ts"],
            ),
        )
    conn.commit()


def _fake_evaluar(respuestas: dict):
    async def _evaluar(game_id, champ_id, date_start, ahora_ts, conn):
        return respuestas[game_id]

    return _evaluar


def _run(coro):
    return asyncio.run(coro)


# --- 1. Ticket simple ganado -------------------------------------------------


def test_ticket_simple_ganado_genera_payout(conn):
    _crear_usuario(conn, usuario_id=1, moneda="CUP", saldo_cent=50000)
    _crear_ticket(
        conn,
        ticket_id=1,
        usuario_id=1,
        moneda="CUP",
        stake_cent=1000,
        selecciones=[
            {
                "game_id": 111,
                "champ_id": 1,
                "market_type": 1,  # 1X2 local
                "parametro_centesimas": None,
                "cuota_milesimas": 1500,  # 1.500
                "inicio_ts": CREADO_TS,
            }
        ],
    )
    respuestas = {111: ResultadoEvaluado(EstadoResultado.CONFIRMADO, "2:0 (1:0,1:0)", "confirmado")}

    with patch.object(cascada_fuentes, "evaluar", _fake_evaluar(respuestas)):
        resultado = _run(settlement_engine.liquidar_ticket(1, AHORA_TS, conn))

    assert resultado.estado == EstadoTicket.GANADO
    assert resultado.movimiento_insertado is True

    ticket = conn.execute("SELECT estado FROM tickets WHERE id = 1").fetchone()
    assert ticket[0] == "ganado"

    mov = conn.execute("SELECT tipo, centavos FROM movimientos WHERE ticket_id = 1").fetchall()
    assert mov == [("payout", 1500)]  # 1000 * 1.500 = 1500

    saldo = conn.execute("SELECT centavos FROM saldos WHERE usuario_id = 1 AND moneda = 'CUP'").fetchone()
    assert saldo[0] == 51500  # 50000 + 1500


# --- 2. Ticket simple perdido ------------------------------------------------


def test_ticket_simple_perdido_sin_movimiento(conn):
    _crear_usuario(conn, usuario_id=1, moneda="CUP", saldo_cent=50000)
    _crear_ticket(
        conn,
        ticket_id=1,
        usuario_id=1,
        moneda="CUP",
        stake_cent=1000,
        selecciones=[
            {"game_id": 111, "champ_id": 1, "market_type": 1, "cuota_milesimas": 1500, "inicio_ts": CREADO_TS}
        ],
    )
    respuestas = {111: ResultadoEvaluado(EstadoResultado.CONFIRMADO, "0:2 (0:1,0:1)", "confirmado")}

    with patch.object(cascada_fuentes, "evaluar", _fake_evaluar(respuestas)):
        resultado = _run(settlement_engine.liquidar_ticket(1, AHORA_TS, conn))

    assert resultado.estado == EstadoTicket.PERDIDO
    assert resultado.movimiento_insertado is False

    assert conn.execute("SELECT estado FROM tickets WHERE id = 1").fetchone()[0] == "perdido"
    assert conn.execute("SELECT COUNT(*) FROM movimientos WHERE ticket_id = 1").fetchone()[0] == 0
    saldo = conn.execute("SELECT centavos FROM saldos WHERE usuario_id = 1 AND moneda = 'CUP'").fetchone()
    assert saldo[0] == 50000  # intacto


# --- 3. Ticket nulo (todas nulas) -------------------------------------------


def test_ticket_nulo_genera_devolucion_por_el_stake(conn):
    _crear_usuario(conn, usuario_id=1, moneda="CUP", saldo_cent=50000)
    _crear_ticket(
        conn,
        ticket_id=1,
        usuario_id=1,
        moneda="CUP",
        stake_cent=1000,
        selecciones=[
            {
                "game_id": 111,
                "champ_id": 1,
                "market_type": 9,  # total mas de
                "parametro_centesimas": 200,  # 2.0
                "cuota_milesimas": 1900,
                "inicio_ts": CREADO_TS,
            }
        ],
    )
    # 1:1 -> total 2, igual al parametro -> push (NULA)
    respuestas = {111: ResultadoEvaluado(EstadoResultado.CONFIRMADO, "1:1 (1:0,0:1)", "confirmado")}

    with patch.object(cascada_fuentes, "evaluar", _fake_evaluar(respuestas)):
        resultado = _run(settlement_engine.liquidar_ticket(1, AHORA_TS, conn))

    assert resultado.estado == EstadoTicket.NULO
    assert resultado.movimiento_insertado is True

    assert conn.execute("SELECT estado FROM tickets WHERE id = 1").fetchone()[0] == "nulo"
    mov = conn.execute("SELECT tipo, centavos FROM movimientos WHERE ticket_id = 1").fetchall()
    assert mov == [("devolucion", 1000)]
    saldo = conn.execute("SELECT centavos FROM saldos WHERE usuario_id = 1 AND moneda = 'CUP'").fetchone()
    assert saldo[0] == 51000  # 50000 + 1000 (el stake devuelto)


# --- 4. Combinada con una pata NO_CONFIRMADO --------------------------------


def test_combinada_con_pata_no_confirmado_queda_pendiente(conn):
    _crear_usuario(conn, usuario_id=1, moneda="CUP", saldo_cent=50000)
    _crear_ticket(
        conn,
        ticket_id=1,
        usuario_id=1,
        moneda="CUP",
        stake_cent=1000,
        selecciones=[
            {"game_id": 111, "champ_id": 1, "market_type": 1, "cuota_milesimas": 1500, "inicio_ts": CREADO_TS},
            {"game_id": 222, "champ_id": 2, "market_type": 1, "cuota_milesimas": 1300, "inicio_ts": CREADO_TS},
        ],
    )
    respuestas = {
        111: ResultadoEvaluado(EstadoResultado.CONFIRMADO, "2:0 (1:0,1:0)", "confirmado"),
        222: ResultadoEvaluado(EstadoResultado.NO_CONFIRMADO, None, "todavía en curso"),
    }

    with patch.object(cascada_fuentes, "evaluar", _fake_evaluar(respuestas)):
        resultado = _run(settlement_engine.liquidar_ticket(1, AHORA_TS, conn))

    assert resultado.estado == EstadoTicket.PENDIENTE
    assert resultado.movimiento_insertado is False

    assert conn.execute("SELECT estado FROM tickets WHERE id = 1").fetchone()[0] == "pendiente"
    assert conn.execute("SELECT COUNT(*) FROM movimientos WHERE ticket_id = 1").fetchone()[0] == 0
    # Ninguna seleccion se toca todavia (ni siquiera la que si confirmo)
    estados_sel = {
        fila[0] for fila in conn.execute("SELECT estado FROM selecciones WHERE ticket_id = 1")
    }
    assert estados_sel == {"pendiente"}


# --- 5. Ticket con una pata REQUIERE_ADMIN ----------------------------------


def test_ticket_con_pata_requiere_admin(conn):
    _crear_usuario(conn, usuario_id=1, moneda="CUP", saldo_cent=50000)
    _crear_ticket(
        conn,
        ticket_id=1,
        usuario_id=1,
        moneda="CUP",
        stake_cent=1000,
        selecciones=[
            {"game_id": 111, "champ_id": 1, "market_type": 1, "cuota_milesimas": 1500, "inicio_ts": CREADO_TS}
        ],
    )
    respuestas = {
        111: ResultadoEvaluado(EstadoResultado.REQUIERE_ADMIN, None, "sportId no soportado"),
    }

    with patch.object(cascada_fuentes, "evaluar", _fake_evaluar(respuestas)):
        resultado = _run(settlement_engine.liquidar_ticket(1, AHORA_TS, conn))

    assert resultado.estado == EstadoTicket.REQUIERE_ADMIN
    assert resultado.movimiento_insertado is False
    assert conn.execute("SELECT estado FROM tickets WHERE id = 1").fetchone()[0] == "requiere_admin"
    assert conn.execute("SELECT COUNT(*) FROM movimientos WHERE ticket_id = 1").fetchone()[0] == 0


# --- Defensa en profundidad: CONFIRMADO con marcador no parseable ----------


def test_confirmado_con_marcador_no_parseable_degrada_a_requiere_admin(conn):
    _crear_usuario(conn, usuario_id=1, moneda="CUP", saldo_cent=50000)
    _crear_ticket(
        conn,
        ticket_id=1,
        usuario_id=1,
        moneda="CUP",
        stake_cent=1000,
        selecciones=[
            {"game_id": 111, "champ_id": 1, "market_type": 1, "cuota_milesimas": 1500, "inicio_ts": CREADO_TS}
        ],
    )
    # cascada_fuentes nunca deberia devolver esto en CONFIRMADO, pero esta
    # capa no confia en esa garantia sin verificarla ella misma.
    respuestas = {111: ResultadoEvaluado(EstadoResultado.CONFIRMADO, "texto raro", "confirmado")}

    with patch.object(cascada_fuentes, "evaluar", _fake_evaluar(respuestas)):
        resultado = _run(settlement_engine.liquidar_ticket(1, AHORA_TS, conn))

    assert resultado.estado == EstadoTicket.REQUIERE_ADMIN


# --- 6. Idempotencia ---------------------------------------------------------


def test_liquidar_dos_veces_un_solo_payout(conn):
    _crear_usuario(conn, usuario_id=1, moneda="CUP", saldo_cent=50000)
    _crear_ticket(
        conn,
        ticket_id=1,
        usuario_id=1,
        moneda="CUP",
        stake_cent=1000,
        selecciones=[
            {"game_id": 111, "champ_id": 1, "market_type": 1, "cuota_milesimas": 1500, "inicio_ts": CREADO_TS}
        ],
    )
    respuestas = {111: ResultadoEvaluado(EstadoResultado.CONFIRMADO, "2:0 (1:0,1:0)", "confirmado")}

    with patch.object(cascada_fuentes, "evaluar", _fake_evaluar(respuestas)):
        primera = _run(settlement_engine.liquidar_ticket(1, AHORA_TS, conn))
        segunda = _run(settlement_engine.liquidar_ticket(1, AHORA_TS + 100, conn))

    assert primera.estado == EstadoTicket.GANADO
    assert primera.movimiento_insertado is True
    assert segunda.estado == EstadoTicket.GANADO
    assert segunda.movimiento_insertado is False  # corte temprano, ni evalua

    total = conn.execute(
        "SELECT COUNT(*) FROM movimientos WHERE ticket_id = 1 AND tipo = 'payout'"
    ).fetchone()[0]
    assert total == 1

    saldo = conn.execute("SELECT centavos FROM saldos WHERE usuario_id = 1 AND moneda = 'CUP'").fetchone()
    assert saldo[0] == 51500  # no se acredito dos veces


# --- 7. Guarda de arranque ---------------------------------------------------


def test_guarda_de_arranque_bloquea_primera_vuelta_y_libera_la_segunda(conn, monkeypatch):
    arranque_ts = 1_700_040_000
    monkeypatch.setattr(settlement_engine, "ARRANQUE_TS", arranque_ts)

    _crear_usuario(conn, usuario_id=1, moneda="CUP", saldo_cent=50000)
    _crear_ticket(
        conn,
        ticket_id=1,
        usuario_id=1,
        moneda="CUP",
        stake_cent=1000,
        selecciones=[
            {"game_id": 111, "champ_id": 1, "market_type": 1, "cuota_milesimas": 1500, "inicio_ts": CREADO_TS}
        ],
    )
    # Observacion previa a este proceso: ultima_consulta_ts ANTERIOR al arranque.
    conn.execute(
        "INSERT INTO observaciones_resultado (game_id, marcador_raw, visto_primera_vez_ts, ultima_consulta_ts) "
        "VALUES (111, '2:0 (1:0,1:0)', ?, ?)",
        (arranque_ts - 5000, arranque_ts - 100),
    )
    conn.commit()

    respuestas = {111: ResultadoEvaluado(EstadoResultado.CONFIRMADO, "2:0 (1:0,1:0)", "confirmado")}

    with patch.object(cascada_fuentes, "evaluar", _fake_evaluar(respuestas)):
        primera = _run(settlement_engine.liquidar_ticket(1, AHORA_TS, conn))

    assert primera.estado == EstadoTicket.PENDIENTE
    assert conn.execute("SELECT estado FROM tickets WHERE id = 1").fetchone()[0] == "pendiente"
    assert conn.execute("SELECT COUNT(*) FROM movimientos WHERE ticket_id = 1").fetchone()[0] == 0

    # Simula la consulta fresca que un evaluar() real habria dejado (el fake
    # no toca observaciones_resultado, asi que se replica el efecto a mano).
    conn.execute(
        "UPDATE observaciones_resultado SET ultima_consulta_ts = ? WHERE game_id = 111",
        (AHORA_TS,),
    )
    conn.commit()

    with patch.object(cascada_fuentes, "evaluar", _fake_evaluar(respuestas)):
        segunda = _run(settlement_engine.liquidar_ticket(1, AHORA_TS + 1000, conn))

    assert segunda.estado == EstadoTicket.GANADO
    assert segunda.movimiento_insertado is True


def test_guarda_de_arranque_no_aplica_sin_fila_previa(conn, monkeypatch):
    """Un partido nunca visto no necesita el ciclo extra: lo protege la
    estabilidad de 15 min de cascada_fuentes."""
    monkeypatch.setattr(settlement_engine, "ARRANQUE_TS", 1_700_040_000)

    _crear_usuario(conn, usuario_id=1, moneda="CUP", saldo_cent=50000)
    _crear_ticket(
        conn,
        ticket_id=1,
        usuario_id=1,
        moneda="CUP",
        stake_cent=1000,
        selecciones=[
            {"game_id": 111, "champ_id": 1, "market_type": 1, "cuota_milesimas": 1500, "inicio_ts": CREADO_TS}
        ],
    )
    respuestas = {111: ResultadoEvaluado(EstadoResultado.CONFIRMADO, "2:0 (1:0,1:0)", "confirmado")}

    with patch.object(cascada_fuentes, "evaluar", _fake_evaluar(respuestas)):
        resultado = _run(settlement_engine.liquidar_ticket(1, AHORA_TS, conn))

    assert resultado.estado == EstadoTicket.GANADO


# --- 8. El ledger cuadra tras liquidar un lote mixto ------------------------


def test_ledger_cuadra_tras_liquidar_lote_mixto(conn):
    _crear_usuario(conn, usuario_id=1, moneda="CUP", saldo_cent=0)
    # Deposito inicial: se registra como movimiento, no se materializa directo.
    conn.execute(
        "INSERT INTO movimientos (usuario_id, moneda, centavos, tipo, ts) VALUES (1, 'CUP', 100000, 'deposito', ?)",
        (CREADO_TS,),
    )
    conn.execute("UPDATE saldos SET centavos = 100000 WHERE usuario_id = 1 AND moneda = 'CUP'")
    conn.commit()

    tickets = [
        (1, 1000, 111, 1, None, 1500, "2:0 (1:0,1:0)"),   # gana: payout 1500
        (2, 1000, 222, 1, None, 1500, "0:2 (0:1,0:1)"),   # pierde: sin movimiento
        (3, 1000, 333, 9, 200, 1900, "1:1 (1:0,0:1)"),    # nulo: devolucion 1000
    ]
    respuestas = {}
    for ticket_id, stake_cent, game_id, market_type, parametro_centesimas, cuota_milesimas, marcador in tickets:
        _crear_ticket(
            conn,
            ticket_id=ticket_id,
            usuario_id=1,
            moneda="CUP",
            stake_cent=stake_cent,
            selecciones=[
                {
                    "game_id": game_id,
                    "champ_id": 1,
                    "market_type": market_type,
                    "parametro_centesimas": parametro_centesimas,
                    "cuota_milesimas": cuota_milesimas,
                    "inicio_ts": CREADO_TS,
                }
            ],
        )
        conn.execute(
            "INSERT INTO movimientos (usuario_id, moneda, centavos, tipo, ticket_id, ts) "
            "VALUES (1, 'CUP', ?, 'stake', ?, ?)",
            (-stake_cent, ticket_id, CREADO_TS),
        )
        conn.execute(
            "UPDATE saldos SET centavos = centavos - ? WHERE usuario_id = 1 AND moneda = 'CUP'",
            (stake_cent,),
        )
        respuestas[game_id] = ResultadoEvaluado(EstadoResultado.CONFIRMADO, marcador, "confirmado")
    conn.commit()

    with patch.object(cascada_fuentes, "evaluar", _fake_evaluar(respuestas)):
        resultados = _run(settlement_engine.liquidar_pendientes(AHORA_TS, conn))

    assert {r.estado for r in resultados} == {EstadoTicket.GANADO, EstadoTicket.PERDIDO, EstadoTicket.NULO}

    suma_movimientos = conn.execute(
        "SELECT SUM(centavos) FROM movimientos WHERE usuario_id = 1 AND moneda = 'CUP'"
    ).fetchone()[0]
    saldo = conn.execute("SELECT centavos FROM saldos WHERE usuario_id = 1 AND moneda = 'CUP'").fetchone()[0]
    assert suma_movimientos == saldo
    assert saldo == 100000 - 3000 + 1500 + 1000  # deposito - 3 stakes + payout + devolucion
