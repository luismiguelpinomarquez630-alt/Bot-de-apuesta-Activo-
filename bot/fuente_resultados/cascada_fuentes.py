"""Cascada de fuentes de resultados.

CLAUDE.md regla 3: este es el ÚNICO módulo que decide si un resultado está
`confirmado`, `no_confirmado` o `requiere_admin`. Ningún otro módulo determina
eso por su cuenta. Implementado contra ESPECIFICACION_FUENTE.md §11 y §12.

No escribe en `tickets`, `selecciones`, `saldos` ni `movimientos` — eso es
`core/apuestas.py` y `core/settlement_engine.py`. Este módulo solo lee 1x (vía
`cliente_1x.py`, nunca directo) y lee/escribe `observaciones_resultado`. No
resuelve mercados: eso es `settlement_engine.py`, contra `REGLAS_LIQUIDACION.md`.
"""

import sqlite3
from dataclasses import dataclass
from enum import Enum

from bot.fuente_resultados.primaria import cliente_1x

# Cascada de fuentes de resultados. Hoy opera con UNA sola fuente: no hay
# fallback implementado todavía (bot/fuente_resultados/fallback/ está vacío,
# ESPECIFICACION_FUENTE.md §12 — "nota de arquitectura"). Es una limitación
# real, no un detalle de implementación: 1x es a la vez fuente de líneas y de
# resultados, un único punto de fallo. Punto de extensión: cuando se agregue
# una segunda fuente acá, una discrepancia entre ambas es REQUIERE_ADMIN, no
# un criterio de desempate silencioso.
FUENTES = (cliente_1x,)

DEPORTE_FUTBOL = 1  # REGLAS_LIQUIDACION §1: solo fútbol en Fase 1

SEGUNDOS_SIN_RESULTADO_REQUIERE_ADMIN = 86400  # 24h, ESPECIFICACION_FUENTE §10
SEGUNDOS_PARTIDO_MINIMO_PARA_CONFIRMAR = 7200  # 2h, ESPECIFICACION_FUENTE §11
SEGUNDOS_ESTABILIDAD_MARCADOR = 900  # 15 min, ESPECIFICACION_FUENTE §11


class EstadoResultado(Enum):
    CONFIRMADO = "confirmado"
    NO_CONFIRMADO = "no_confirmado"
    REQUIERE_ADMIN = "requiere_admin"


@dataclass(frozen=True)
class ResultadoEvaluado:
    estado: EstadoResultado
    marcador_raw: str | None
    motivo: str


def _tiene_prorroga(marcador: dict) -> bool:
    """Más de 2 periodos en periodos_raw es indicio de prórroga
    (REGLAS_LIQUIDACION §3): 1X2, totales y hándicaps se resuelven al final
    del tiempo reglamentario, sin prórroga ni penales."""
    periodos_raw = marcador.get("periodos_raw") or ""
    if not periodos_raw:
        return False
    return len(periodos_raw.split(",")) > 2


def _registrar_observacion(
    conn: sqlite3.Connection, game_id: int, marcador_raw: str, ahora_ts: int
) -> int:
    """Aplica la semántica de ESQUEMA_DB.md §2.1 y devuelve el
    visto_primera_vez_ts vigente después de escribir.

    Marcador igual al guardado -> mantiene visto_primera_vez_ts, solo
    actualiza ultima_consulta_ts. Marcador distinto -> resetea
    visto_primera_vez_ts a ahora_ts.
    """
    fila = conn.execute(
        "SELECT marcador_raw, visto_primera_vez_ts FROM observaciones_resultado "
        "WHERE game_id = ?",
        (game_id,),
    ).fetchone()

    if fila is not None and fila[0] == marcador_raw:
        visto_primera_vez_ts = fila[1]
        conn.execute(
            "UPDATE observaciones_resultado SET ultima_consulta_ts = ? WHERE game_id = ?",
            (ahora_ts, game_id),
        )
    else:
        visto_primera_vez_ts = ahora_ts
        conn.execute(
            "INSERT INTO observaciones_resultado "
            "(game_id, marcador_raw, visto_primera_vez_ts, ultima_consulta_ts) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(game_id) DO UPDATE SET "
            "marcador_raw = excluded.marcador_raw, "
            "visto_primera_vez_ts = excluded.visto_primera_vez_ts, "
            "ultima_consulta_ts = excluded.ultima_consulta_ts",
            (game_id, marcador_raw, ahora_ts, ahora_ts),
        )
    conn.commit()
    return visto_primera_vez_ts


async def evaluar(
    game_id: int,
    champ_id: int,
    date_start: int,
    ahora_ts: int,
    conn: sqlite3.Connection,
) -> ResultadoEvaluado:
    """Evalúa el estado de un resultado. El primer guarda que dispara, gana."""
    items = await cliente_1x.obtener_partidos(
        champ_id, date_start, date_start + cliente_1x.VENTANA_MAX_S
    )
    item = next((i for i in items if i.get("id") == game_id), None)

    if item is None:
        if ahora_ts - date_start >= SEGUNDOS_SIN_RESULTADO_REQUIERE_ADMIN:
            return ResultadoEvaluado(
                EstadoResultado.REQUIERE_ADMIN,
                None,
                "el partido no aparece en resultados 24h después del inicio",
            )
        return ResultadoEvaluado(
            EstadoResultado.NO_CONFIRMADO,
            None,
            "el partido todavía no aparece en resultados",
        )

    if item.get("sportId") != DEPORTE_FUTBOL:
        return ResultadoEvaluado(
            EstadoResultado.REQUIERE_ADMIN,
            None,
            f"sportId {item.get('sportId')} no soportado en Fase 1",
        )

    if not cliente_1x.es_partido_real(item):
        return ResultadoEvaluado(
            EstadoResultado.REQUIERE_ADMIN,
            None,
            "el item es un agregado de liga o un evento combinado",
        )

    marcador = cliente_1x.parse_score(item.get("score"))
    if marcador is None:
        return ResultadoEvaluado(
            EstadoResultado.REQUIERE_ADMIN,
            None,
            f"score no reconocido: {item.get('score')!r}",
        )

    if _tiene_prorroga(marcador):
        return ResultadoEvaluado(
            EstadoResultado.REQUIERE_ADMIN,
            item["score"],
            "más de 2 periodos en periodos_raw, indicio de prórroga",
        )

    visto_primera_vez_ts = _registrar_observacion(conn, game_id, item["score"], ahora_ts)

    if ahora_ts - date_start < SEGUNDOS_PARTIDO_MINIMO_PARA_CONFIRMAR:
        return ResultadoEvaluado(
            EstadoResultado.NO_CONFIRMADO,
            item["score"],
            "todavía no pasaron 2h desde el inicio del partido",
        )

    if ahora_ts - visto_primera_vez_ts < SEGUNDOS_ESTABILIDAD_MARCADOR:
        return ResultadoEvaluado(
            EstadoResultado.NO_CONFIRMADO,
            item["score"],
            "el marcador todavía no estuvo estable 15 minutos",
        )

    return ResultadoEvaluado(
        EstadoResultado.CONFIRMADO,
        item["score"],
        "marcador estable y partido pasado su duración normal",
    )
