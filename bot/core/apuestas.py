"""Aceptación de apuestas simples y combinadas.

CAPA 2. Segunda puerta por la que se mueve dinero (la primera es
`settlement_engine.py`). Junta `cache_cuotas` (CAPA 1), `exposicion`
(CAPA 2) y `dominio/` (CAPA 0) — no reimplementa ninguno.

Decisiones de diseño:

- La exposición se valida contra el payout REAL: stake × cuota efectiva
  calculada con las cuotas FRESCAS de la caché al sellar, no las que vio el
  usuario (CACHE_CUOTAS.md §9).
- Una combinada con cualquier pata sin cuota fresca en la caché se rechaza
  ENTERA. No se sella parcial, no se espera.
- Si cualquier pata cae en "baja > UMBRAL_BAJA", el ticket ENTERO pasa a
  RECONFIRMAR: nada se sella, nada se escribe. Se devuelven las cuotas
  vigentes de TODAS las patas (no solo la que bajó), para que el usuario
  reconfirme el combo completo de una vez — mismo criterio de todo-o-nada
  que la regla anterior, aplicado consistentemente.
- Reducción automática de stake al exceder `PAYOUT_MAX_TICKET` (LIMITES.md
  §3, "reducir stake automáticamente hasta el tope"): NO implementada en
  Fase 1. Se rechaza directo. Pendiente para más adelante.
"""

import sqlite3
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

from bot.core.exposicion import BANCAS, cabe_apuesta
from bot.dominio.combinadas import combinada_valida
from bot.dominio.mercados import linea_a_centesimas
from bot.dominio.tiempo import inicio_de_dia_habana
from bot.fuente_resultados.cache_cuotas import UMBRAL_BAJA, obtener_cuota_fresca

MIL = Decimal("1000")

# LIMITES.md §2. STAKE_MIN no es un % de banca (columna "—" en la tabla), el
# resto sí y se deriva de BANCAS (bot/core/exposicion.py) por aritmética
# entera, sin float.
STAKE_MIN = {"CUP": 20_00, "USD": 50}
STAKE_MAX_SIMPLE = {moneda: banca * 1 // 100 for moneda, banca in BANCAS.items()}
PAYOUT_MAX_TICKET = {moneda: banca * 5 // 100 for moneda, banca in BANCAS.items()}
STAKE_DIARIO_MAX_USUARIO = {moneda: banca * 4 // 100 for moneda, banca in BANCAS.items()}
CUOTA_MAX_ACEPTADA_MILESIMAS = 20_000  # LIMITES.md §2, cuota 20,0


@dataclass(frozen=True)
class SeleccionEntrada:
    game_id: int
    champ_id: int
    sport_id: int
    market_type: int
    parametro: Decimal | None
    cuota_vista_milesimas: int
    inicio_ts: int
    equipo_local: str
    equipo_visitante: str


class EstadoAceptacion(Enum):
    ACEPTADA = "aceptada"
    RECHAZADA = "rechazada"
    RECONFIRMAR = "reconfirmar"


@dataclass(frozen=True)
class CuotaReconfirmacion:
    game_id: int
    market_type: int
    parametro: Decimal | None
    cuota_vista_milesimas: int
    cuota_vigente_milesimas: int


@dataclass(frozen=True)
class ResultadoAceptacion:
    estado: EstadoAceptacion
    motivo: str
    ticket_id: int | None = None
    cuotas_para_reconfirmar: list[CuotaReconfirmacion] | None = None


def cuota_efectiva_milesimas(cuotas_milesimas: list[int]) -> int:
    """Producto de las cuotas selladas, redondeado a milésimas
    (REGLAS_LIQUIDACION.md §7.1). Asume que TODAS ganan — es la cuota "de
    catálogo" del combo (`tickets.cuota_milesimas`). Distinta de la función
    del mismo nombre en `liquidacion_tickets.py`, que en la liquidación real
    solo cuenta las patas que efectivamente ganaron.

    Misma función que usará el display: no se duplica este cálculo.
    """
    producto = Decimal(1)
    for c in cuotas_milesimas:
        producto *= Decimal(c) / MIL
    return int((producto * MIL).to_integral_value(ROUND_HALF_UP))


def payout_combinada_cent(stake_cent: int, cuota_efectiva_ms: int) -> int:
    """`stake × cuota_efectiva`, ROUND_HALF_UP a centavos (REGLAS_LIQUIDACION
    §7.1). Misma función que usará el display."""
    return int((Decimal(stake_cent) * Decimal(cuota_efectiva_ms) / MIL).to_integral_value(ROUND_HALF_UP))


def _sellar_cuota(vista_ms: int, vigente_ms: int) -> int | None:
    """Política de precio, CACHE_CUOTAS.md §9. None si la baja supera
    UMBRAL_BAJA: esa pata necesita reconfirmación explícita del usuario.

    Nunca se sella por encima de la vigente: si mejoró, se sella a la vista
    (menor); la mejora se la queda la banca.
    """
    if vigente_ms >= vista_ms:
        return vista_ms
    baja = (Decimal(vista_ms) - Decimal(vigente_ms)) / Decimal(vista_ms)
    if baja <= UMBRAL_BAJA:
        return vigente_ms
    return None


def aceptar_apuesta(
    conn: sqlite3.Connection,
    usuario_id: int,
    moneda: str,
    stake_cent: int,
    selecciones: list[SeleccionEntrada],
    ahora_ts: int,
) -> ResultadoAceptacion:
    """Acepta una apuesta simple (1 selección) o combinada (2..COMBI_MAX_PATAS).

    Validaciones fuera de la transacción (no tocan saldos), en orden:
    estructura de combinada → cuota fresca por pata → política de precio
    (sellado o reconfirmación) → cuota efectiva y payout → límites de
    ticket. Recién entonces se abre BEGIN IMMEDIATE, donde se relee el saldo,
    se recalcula la exposición CON esta apuesta incluida y se escribe todo
    junto (LIMITES.md §9): si la exposición se comprobara fuera de la
    transacción, dos apuestas simultáneas al mismo evento pasarían las dos y
    el límite se saltaría.
    """
    if not selecciones:
        raise ValueError("una apuesta necesita al menos una selección")

    game_ids = [s.game_id for s in selecciones]

    if len(selecciones) > 1:
        cuota_total_vista_ms = cuota_efectiva_milesimas([s.cuota_vista_milesimas for s in selecciones])
        if not combinada_valida(game_ids, cuota_total_vista_ms):
            return ResultadoAceptacion(
                EstadoAceptacion.RECHAZADA,
                "la combinada no cumple LIMITES.md §7 (cantidad de patas, "
                "partido repetido o cuota máxima)",
            )

    cuotas_vigentes = []
    for s in selecciones:
        vigente = obtener_cuota_fresca(s.game_id, s.market_type, s.parametro, ahora_ts)
        if vigente is None:
            return ResultadoAceptacion(
                EstadoAceptacion.RECHAZADA,
                "una selección ya no está disponible, armá la apuesta de nuevo",
            )
        cuotas_vigentes.append(vigente)

    selladas_ms = [
        _sellar_cuota(s.cuota_vista_milesimas, v.cuota_milesimas)
        for s, v in zip(selecciones, cuotas_vigentes)
    ]

    if any(sellada is None for sellada in selladas_ms):
        cuotas_para_reconfirmar = [
            CuotaReconfirmacion(
                game_id=s.game_id,
                market_type=s.market_type,
                parametro=s.parametro,
                cuota_vista_milesimas=s.cuota_vista_milesimas,
                cuota_vigente_milesimas=v.cuota_milesimas,
            )
            for s, v in zip(selecciones, cuotas_vigentes)
        ]
        return ResultadoAceptacion(
            EstadoAceptacion.RECONFIRMAR,
            "una o más cuotas bajaron más de lo tolerado, reconfirmá para continuar",
            cuotas_para_reconfirmar=cuotas_para_reconfirmar,
        )

    cuota_ticket_ms = cuota_efectiva_milesimas(selladas_ms)
    payout_cent = payout_combinada_cent(stake_cent, cuota_ticket_ms)

    if stake_cent < STAKE_MIN[moneda]:
        return ResultadoAceptacion(EstadoAceptacion.RECHAZADA, "el stake no llega al mínimo (LIMITES.md §2)")
    if len(selecciones) == 1 and stake_cent > STAKE_MAX_SIMPLE[moneda]:
        return ResultadoAceptacion(
            EstadoAceptacion.RECHAZADA, "el stake supera STAKE_MAX_SIMPLE para una apuesta simple"
        )
    if cuota_ticket_ms > CUOTA_MAX_ACEPTADA_MILESIMAS:
        return ResultadoAceptacion(EstadoAceptacion.RECHAZADA, "la cuota supera CUOTA_MAX_ACEPTADA")
    if payout_cent > PAYOUT_MAX_TICKET[moneda]:
        # LIMITES.md §3 permite reducir el stake automáticamente hasta el
        # tope en vez de rechazar. NO implementado en Fase 1: pendiente.
        return ResultadoAceptacion(EstadoAceptacion.RECHAZADA, "el payout supera PAYOUT_MAX_TICKET")

    conn.execute("BEGIN IMMEDIATE")

    fila_saldo = conn.execute(
        "SELECT centavos FROM saldos WHERE usuario_id = ? AND moneda = ?", (usuario_id, moneda)
    ).fetchone()
    saldo_cent = fila_saldo[0] if fila_saldo else 0
    if saldo_cent < stake_cent:
        conn.rollback()
        return ResultadoAceptacion(EstadoAceptacion.RECHAZADA, "saldo insuficiente")

    resultado_exposicion = cabe_apuesta(conn, moneda, game_ids, payout_cent, ahora_ts)
    if not resultado_exposicion.cabe:
        conn.rollback()
        return ResultadoAceptacion(EstadoAceptacion.RECHAZADA, resultado_exposicion.motivo)

    # ⚠️ Asume que no hay cancelación de apuestas (Fase 1): suma todos los
    # movimientos 'stake' del día sin descontar nada. Si se añaden
    # cancelaciones más adelante, esta query hay que revisarla.
    inicio_hoy = inicio_de_dia_habana(ahora_ts)
    fila_stake_hoy = conn.execute(
        "SELECT COALESCE(SUM(-centavos), 0) FROM movimientos "
        "WHERE usuario_id = ? AND moneda = ? AND tipo = 'stake' AND ts >= ?",
        (usuario_id, moneda, inicio_hoy),
    ).fetchone()
    if fila_stake_hoy[0] + stake_cent > STAKE_DIARIO_MAX_USUARIO[moneda]:
        conn.rollback()
        return ResultadoAceptacion(EstadoAceptacion.RECHAZADA, "supera STAKE_DIARIO_MAX_USUARIO")

    cursor = conn.execute(
        "INSERT INTO tickets (usuario_id, moneda, stake_cent, cuota_milesimas, "
        "payout_pot_cent, estado, creado_ts) VALUES (?, ?, ?, ?, ?, 'pendiente', ?)",
        (usuario_id, moneda, stake_cent, cuota_ticket_ms, payout_cent, ahora_ts),
    )
    ticket_id = cursor.lastrowid

    for s, sellada_ms in zip(selecciones, selladas_ms):
        parametro_centesimas = linea_a_centesimas(s.parametro) if s.parametro is not None else None
        conn.execute(
            "INSERT INTO selecciones (ticket_id, game_id, champ_id, sport_id, market_type, "
            "parametro_centesimas, cuota_milesimas, equipo_local, equipo_visitante, inicio_ts, estado) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pendiente')",
            (
                ticket_id,
                s.game_id,
                s.champ_id,
                s.sport_id,
                s.market_type,
                parametro_centesimas,
                sellada_ms,
                s.equipo_local,
                s.equipo_visitante,
                s.inicio_ts,
            ),
        )

    conn.execute(
        "INSERT INTO movimientos (usuario_id, moneda, centavos, tipo, ticket_id, ts) "
        "VALUES (?, ?, ?, 'stake', ?, ?)",
        (usuario_id, moneda, -stake_cent, ticket_id, ahora_ts),
    )
    conn.execute(
        "UPDATE saldos SET centavos = centavos - ? WHERE usuario_id = ? AND moneda = ?",
        (stake_cent, usuario_id, moneda),
    )

    conn.commit()
    return ResultadoAceptacion(EstadoAceptacion.ACEPTADA, "apuesta aceptada", ticket_id=ticket_id)
