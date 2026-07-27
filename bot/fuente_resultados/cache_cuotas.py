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

    Flujo de dos pasos (ESPECIFICACION_FUENTE §0): provider exige
    `champs=<champ_id>` en `Get1x2_VZip`, no permite barrer un deporte
    entero. Primero se listan las ligas (`obtener_ligas_con_cuotas`) y
    después se pide cada una, una por una — secuencial, no concurrente: no
    tiene sentido golpear a provider con N pedidos en paralelo justo
    después de haber visto que Cloudflare corta pedidos grandes con 502.

    ⚠️ Contrato revisado (§7): todo-o-nada en el PASO 1, mejor esfuerzo en
    el PASO 2.
      - Paso 1 (listar ligas): si falla, se aborta el refresco entero y se
        conserva el snapshot anterior — sin la lista de ligas no hay nada
        que hacer.
      - Paso 2 (cuotas por liga): una liga individual que falla (puede
        haber pasado a en vivo entre el paso 1 y el paso 2, o no tener feed
        prepartido en este momento) se SALTA y se loguea — no aborta el
        refresco de las demás ligas.
      - Si TODAS las ligas del paso 2 fallan, no se reemplaza el snapshot
        por uno vacío: se conserva el anterior. Un snapshot vacío dejaría
        al bot sin cuotas cuando el problema puede ser transitorio de una
        corrida.

    ⚠️ El PASO 1 devolviendo CERO ligas (sin excepción, la llamada fue
    exitosa) se trata IGUAL que "todas las ligas fallaron": se conserva el
    snapshot anterior, no se reemplaza por uno vacío. No hay forma de
    distinguir desde `Value: []` si de verdad no hay fútbol prepartido
    ahora mismo o si es un hueco momentáneo del listado (provider entre
    actualizaciones) — y vaciar el snapshot activamente rechazaría TODA
    apuesta de inmediato (`obtener_cuota_fresca` → `None` para todo),
    incluso cuotas que a los 5 minutos seguían siendo válidas. El TTL (§6)
    ya cubre el caso genuino: si de verdad no hay ligas, las cuotas viejas
    vencen solas por su `capturada_ts` en 90s. Vaciar solo sería correcto
    si se supiera que las cuotas viejas ya no valen, y desde `Value: []` no
    se sabe eso.

    Las cuotas que ya tenía el snapshot anterior siguen envejeciendo y
    venciendo por su propio `capturada_ts` (§6) mientras no se reemplaza.
    Nunca se sirve nada como si fuera fresco por culpa de un refresco
    fallido o parcial.
    """
    global _snapshot
    try:
        ligas = await cliente_1x.obtener_ligas_con_cuotas(sport_id)
    except Exception:
        _logger.warning(
            "refrescar(sport_id=%s): falló listar ligas (paso 1), se conserva el snapshot anterior",
            sport_id,
            exc_info=True,
        )
        return

    eventos: list[cliente_1x.EventoCuotas] = []
    ligas_ok = 0
    ligas_fallidas = 0
    for liga in ligas:
        try:
            eventos.extend(await cliente_1x.obtener_cuotas(sport_id, liga.champ_id, count))
            ligas_ok += 1
        except Exception:
            ligas_fallidas += 1
            _logger.warning(
                "refrescar(sport_id=%s): liga %s falló en el paso 2, se salta", sport_id, liga.champ_id, exc_info=True
            )
            continue

    if ligas_ok == 0:
        if ligas:
            _logger.warning(
                "refrescar(sport_id=%s): las %d ligas fallaron, se conserva el snapshot anterior",
                sport_id,
                ligas_fallidas,
            )
        else:
            _logger.warning(
                "refrescar(sport_id=%s): el paso 1 devolvió 0 ligas, se conserva el snapshot anterior "
                "(el TTL resuelve el caso genuino)",
                sport_id,
            )
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
