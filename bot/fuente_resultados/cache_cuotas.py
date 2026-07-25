"""Snapshot de cuotas en memoria, con caducidad, para control de precio.

Implementado contra CACHE_CUOTAS.md (CLAUDE.md regla 5). CAPA 1: llama a
`cliente_1x.obtener_cuotas` (misma capa) y expone lectura a `core/`. No
importa de `core/`.

⚠️ No es una caché de rendimiento: si el snapshot está vencido, BLOQUEA la
aceptación (devuelve None), nunca sirve una cuota vieja como si fuera fresca
(CACHE_CUOTAS.md §2). No valida límites ni toca saldos — eso es
`core/apuestas.py`.

En memoria del proceso, no en SQLite (§4): un solo proceso en Fase 1.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal

from bot.fuente_resultados.primaria import cliente_1x

TTL_S = 90  # CACHE_CUOTAS.md §6: Get1x2_VZip contra la fuente real tarda 4-10s
INTERVALO_REFRESCO_S = 45  # CACHE_CUOTAS.md §7: < TTL, sin solapar corridas lentas
UMBRAL_BAJA = Decimal("0.02")

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CuotaVigente:
    game_id: int
    champ_id: int
    market_type: int
    parametro: Decimal | None
    cuota_milesimas: int
    capturada_ts: int  # cuándo se trajo del feed


ClaveMercado = tuple[int, int, Decimal | None]  # game_id, market_type, parametro


@dataclass
class _Snapshot:
    por_clave: dict[ClaveMercado, CuotaVigente]
    sport_id: int
    refrescado_ts: int


_snapshot: _Snapshot | None = None  # None hasta el primer refrescar() exitoso


def _clave(game_id: int, market_type: int, parametro: Decimal | None) -> ClaveMercado:
    return (game_id, market_type, parametro)


async def refrescar(sport_id: int, count: int, ahora_ts: int) -> None:
    """Trae el feed y reemplaza el snapshot de ese deporte (§7).

    Si `obtener_cuotas` falla, el snapshot anterior se conserva tal cual: las
    cuotas que ya tenía siguen envejeciendo y venciendo por su propio
    `capturada_ts` (§6). Nunca se sirve nada como si fuera fresco por culpa de
    un refresco fallido.
    """
    global _snapshot
    try:
        eventos = await cliente_1x.obtener_cuotas(sport_id, count)
    except Exception:
        _logger.warning("refrescar(sport_id=%s) falló, se conserva el snapshot anterior", sport_id, exc_info=True)
        return

    por_clave: dict[ClaveMercado, CuotaVigente] = {}
    for evento in eventos:
        for mercado in evento.mercados:
            clave = _clave(evento.game_id, mercado.tipo, mercado.parametro)
            por_clave[clave] = CuotaVigente(
                game_id=evento.game_id,
                champ_id=evento.champ_id,
                market_type=mercado.tipo,
                parametro=mercado.parametro,
                cuota_milesimas=mercado.cuota_milesimas,
                capturada_ts=ahora_ts,
            )

    _snapshot = _Snapshot(por_clave=por_clave, sport_id=sport_id, refrescado_ts=ahora_ts)


def obtener_cuota_fresca(
    game_id: int,
    market_type: int,
    parametro: Decimal | None,
    ahora_ts: int,
) -> CuotaVigente | None:
    """Devuelve la cuota SOLO si existe y está fresca (§8).

    None si no está en el snapshot o si venció. La frescura se mide sobre
    `capturada_ts` de la propia cuota, no sobre `refrescado_ts` del snapshot
    (§6): un refresco fallido no la rejuvenece.
    """
    if _snapshot is None:
        return None
    cuota = _snapshot.por_clave.get(_clave(game_id, market_type, parametro))
    if cuota is None:
        return None
    if ahora_ts - cuota.capturada_ts > TTL_S:
        return None
    return cuota
