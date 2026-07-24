import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from bot.fuente_resultados import cascada_fuentes
from bot.fuente_resultados.primaria import cliente_1x

FIXTURES = Path(__file__).parents[1] / "fixtures"
MIGRACION_002 = Path(__file__).parents[2] / "bot" / "db" / "migraciones" / "002_observaciones_resultado.sql"

DATE_START = 1_700_000_000


def _item_base():
    return json.loads((FIXTURES / "cascada_item_base.json").read_text())


@pytest.fixture
def conn():
    con = sqlite3.connect(":memory:")
    con.executescript(MIGRACION_002.read_text())
    yield con
    con.close()


def _evaluar(item, ahora_ts, conn, game_id=None, champ_id=None, date_start=None):
    """Mockea cliente_1x.obtener_partidos para devolver [item] (o [] si item
    es None) y llama a evaluar(). Ningún test toca la red."""
    items = [item] if item is not None else []
    with patch.object(cliente_1x, "obtener_partidos", return_value=items):
        return asyncio.run(
            cascada_fuentes.evaluar(
                game_id=game_id if game_id is not None else _item_base()["id"],
                champ_id=champ_id if champ_id is not None else _item_base()["champId"],
                date_start=date_start if date_start is not None else DATE_START,
                ahora_ts=ahora_ts,
                conn=conn,
            )
        )


# --- Rama 2: game_id no está en la respuesta ------------------------------


def test_no_encontrado_antes_de_24h_no_confirmado(conn):
    resultado = _evaluar(item=None, ahora_ts=DATE_START + 3600, conn=conn)
    assert resultado.estado == cascada_fuentes.EstadoResultado.NO_CONFIRMADO
    assert resultado.marcador_raw is None


def test_no_encontrado_despues_de_24h_requiere_admin(conn):
    resultado = _evaluar(item=None, ahora_ts=DATE_START + 86400, conn=conn)
    assert resultado.estado == cascada_fuentes.EstadoResultado.REQUIERE_ADMIN
    assert resultado.marcador_raw is None


# --- Rama 3: sport_id != 1 -------------------------------------------------


def test_sport_id_no_futbol_requiere_admin(conn):
    item = _item_base()
    item["sportId"] = 4  # tenis
    resultado = _evaluar(item=item, ahora_ts=DATE_START + 10800, conn=conn)
    assert resultado.estado == cascada_fuentes.EstadoResultado.REQUIERE_ADMIN


# --- Rama 4: not es_partido_real -------------------------------------------


def test_evento_combinado_requiere_admin(conn):
    item = _item_base()
    item["opp1Ids"] = [111, 112]  # combinado de 2 equipos
    resultado = _evaluar(item=item, ahora_ts=DATE_START + 10800, conn=conn)
    assert resultado.estado == cascada_fuentes.EstadoResultado.REQUIERE_ADMIN


def test_agregado_de_liga_requiere_admin(conn):
    item = _item_base()
    item["dopInfo"] = "5 Partidos"
    resultado = _evaluar(item=item, ahora_ts=DATE_START + 10800, conn=conn)
    assert resultado.estado == cascada_fuentes.EstadoResultado.REQUIERE_ADMIN


# --- Rama 5: parse_score is None -------------------------------------------


def test_score_no_parseable_requiere_admin(conn):
    item = _item_base()
    item["score"] = "texto raro"
    resultado = _evaluar(item=item, ahora_ts=DATE_START + 10800, conn=conn)
    assert resultado.estado == cascada_fuentes.EstadoResultado.REQUIERE_ADMIN


# --- Rama 6: prórroga (más de 2 periodos) ----------------------------------


def test_partido_con_3_periodos_requiere_admin(conn):
    item = _item_base()
    item["score"] = "2:2 (1:1,1:1,0:0)"
    resultado = _evaluar(item=item, ahora_ts=DATE_START + 10800, conn=conn)
    assert resultado.estado == cascada_fuentes.EstadoResultado.REQUIERE_ADMIN
    assert resultado.marcador_raw == "2:2 (1:1,1:1,0:0)"


# --- Rama 8: partido todavía en curso (< 2h) -------------------------------


def test_partido_en_curso_no_confirmado(conn):
    resultado = _evaluar(item=_item_base(), ahora_ts=DATE_START + 3600, conn=conn)
    assert resultado.estado == cascada_fuentes.EstadoResultado.NO_CONFIRMADO
    assert resultado.marcador_raw == "1:1 (1:1,0:0)"


# --- Rama 9: primera observación / estabilidad < 15 min --------------------


def test_primera_observacion_de_un_partido_no_confirmado(conn):
    """Nunca puede confirmar en la primera observación: visto_primera_vez_ts
    queda igual a ahora_ts, así que la ventana de estabilidad es 0."""
    resultado = _evaluar(item=_item_base(), ahora_ts=DATE_START + 10800, conn=conn)
    assert resultado.estado == cascada_fuentes.EstadoResultado.NO_CONFIRMADO
    assert resultado.marcador_raw == "1:1 (1:1,0:0)"

    fila = conn.execute(
        "SELECT visto_primera_vez_ts, ultima_consulta_ts FROM observaciones_resultado WHERE game_id = ?",
        (_item_base()["id"],),
    ).fetchone()
    assert fila == (DATE_START + 10800, DATE_START + 10800)


# --- Rama 10: CONFIRMADO ----------------------------------------------------


def test_marcador_estable_15_min_y_partido_de_hace_3h_confirmado(conn):
    game_id = _item_base()["id"]
    ahora_ts = DATE_START + 10800  # 3h desde el inicio

    conn.execute(
        "INSERT INTO observaciones_resultado "
        "(game_id, marcador_raw, visto_primera_vez_ts, ultima_consulta_ts) "
        "VALUES (?, ?, ?, ?)",
        (game_id, "1:1 (1:1,0:0)", ahora_ts - 1000, ahora_ts - 100),
    )
    conn.commit()

    resultado = _evaluar(item=_item_base(), ahora_ts=ahora_ts, conn=conn)

    assert resultado.estado == cascada_fuentes.EstadoResultado.CONFIRMADO
    assert resultado.marcador_raw == "1:1 (1:1,0:0)"


# --- Cambio de marcador entre consultas: resetea la estabilidad -----------


def test_marcador_cambia_entre_consultas_resetea_estabilidad(conn):
    game_id = _item_base()["id"]
    ahora_ts = DATE_START + 10800  # 3h desde el inicio, pasaría el guard de 2h

    # Observación previa: marcador distinto, "estable" hace mucho tiempo.
    conn.execute(
        "INSERT INTO observaciones_resultado "
        "(game_id, marcador_raw, visto_primera_vez_ts, ultima_consulta_ts) "
        "VALUES (?, ?, ?, ?)",
        (game_id, "0:0 (0:0,0:0)", ahora_ts - 5000, ahora_ts - 100),
    )
    conn.commit()

    item = _item_base()
    item["score"] = "1:1 (1:1,0:0)"  # marcador nuevo, distinto al guardado

    resultado = _evaluar(item=item, ahora_ts=ahora_ts, conn=conn)

    assert resultado.estado == cascada_fuentes.EstadoResultado.NO_CONFIRMADO
    assert resultado.marcador_raw == "1:1 (1:1,0:0)"

    fila = conn.execute(
        "SELECT marcador_raw, visto_primera_vez_ts FROM observaciones_resultado WHERE game_id = ?",
        (game_id,),
    ).fetchone()
    assert fila == ("1:1 (1:1,0:0)", ahora_ts)  # reseteado a ahora_ts


def test_marcador_igual_entre_consultas_mantiene_estabilidad(conn):
    """Control: si el marcador NO cambia, visto_primera_vez_ts no se toca."""
    game_id = _item_base()["id"]
    ahora_ts = DATE_START + 10800

    conn.execute(
        "INSERT INTO observaciones_resultado "
        "(game_id, marcador_raw, visto_primera_vez_ts, ultima_consulta_ts) "
        "VALUES (?, ?, ?, ?)",
        (game_id, "1:1 (1:1,0:0)", ahora_ts - 2000, ahora_ts - 100),
    )
    conn.commit()

    resultado = _evaluar(item=_item_base(), ahora_ts=ahora_ts, conn=conn)

    assert resultado.estado == cascada_fuentes.EstadoResultado.CONFIRMADO

    fila = conn.execute(
        "SELECT visto_primera_vez_ts, ultima_consulta_ts FROM observaciones_resultado WHERE game_id = ?",
        (game_id,),
    ).fetchone()
    assert fila == (ahora_ts - 2000, ahora_ts)  # visto_primera_vez_ts intacto


# --- FUENTES: cascada explícita, hoy con una sola fuente -------------------


def test_fuentes_es_explicita_y_tiene_una_sola_fuente():
    assert cascada_fuentes.FUENTES == (cliente_1x,)
