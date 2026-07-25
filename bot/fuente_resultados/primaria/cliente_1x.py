"""Cliente de solo lectura contra la API de 1x.

Implementado contra ESPECIFICACION_FUENTE.md (CLAUDE.md regla 5). Este módulo
solo obtiene y parsea datos crudos: no decide `confirmado` / `no_confirmado` /
`requiere_admin` (esa decisión es exclusiva de `cascada_fuentes.py`,
CLAUDE.md regla 3).

Cliente asíncrono (httpx): python-telegram-bot v20 corre sobre asyncio, y una
llamada bloqueante con timeout de varios segundos congelaría el event loop
del bot entero mientras dura.
"""

import asyncio
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

import httpx

from bot.dominio.mercados import TIPOS_SOPORTADOS, linea_valida

# bol.1xbet.com no sirve desde datacenter (403 por IP/ASN, verificado desde
# Railway). provider.betfantasy.bet es la fuente real de producción: mismo
# formato, sin auth, responde 200 desde datacenter (ESPECIFICACION_FUENTE §0).
BASE_URL = "https://provider.betfantasy.bet"
REF = 156  # resultados (`ref`) y cuotas (`partner`): 156 en ambos, verificado
LNG = "es"

VENTANA_MAX_S = 86400  # único tamaño de ventana verificado, ESPECIFICACION_FUENTE §2
# Get1x2_VZip contra provider tarda 4-10s y pesa 100+KB (ESPECIFICACION_FUENTE
# §0) — con 10s el timeout llegaba antes que la respuesta y salía como 502.
TIMEOUT_S = 30
REINTENTOS = 3
BACKOFF_BASE_S = 1.0

# Parámetros fijos de LineFeed/Get1x2_VZip contra provider.betfantasy.bet,
# verificados desde la app real (ESPECIFICACION_FUENTE §0). country/gr son
# los de provider, NO los de 1x (97/687).
LINEFEED_CFVIEW = 2
LINEFEED_MODE = 4
LINEFEED_COUNTRY = 94
LINEFEED_GR = 413

_RX_SCORE = re.compile(r"^(\d+):(\d+)\s*(?:\(([^)]*)\))?$")

# Cliente HTTP compartido, para no pagar un handshake TLS nuevo por cada
# petición. Se crea de forma perezosa: en tiempo de import no hay event loop
# corriendo, y httpx.AsyncClient lo necesita.
_cliente: httpx.AsyncClient | None = None


def _get_cliente() -> httpx.AsyncClient:
    global _cliente
    if _cliente is None:
        _cliente = httpx.AsyncClient(timeout=TIMEOUT_S)
    return _cliente


async def cerrar_cliente() -> None:
    """Cierra el cliente HTTP compartido. Llamar al apagar el bot."""
    global _cliente
    if _cliente is not None:
        await _cliente.aclose()
        _cliente = None


def alinear_ts(ts: int) -> int:
    """Los endpoints de resultados exigen múltiplos de 300 s (ESPECIFICACION_FUENTE §2)."""
    return (ts // 300) * 300


def _validar_ventana(desde: int, hasta: int) -> None:
    if hasta - desde > VENTANA_MAX_S:
        raise ValueError(
            f"Ventana de {hasta - desde}s excede el máximo verificado de "
            f"{VENTANA_MAX_S}s (ESPECIFICACION_FUENTE §2)"
        )


async def _get(url: str, params: dict) -> dict:
    """GET con timeout explícito y reintento con backoff exponencial.

    Sin headers de autenticación: la API de resultados es pública
    (ESPECIFICACION_FUENTE §1).

    Solo se reintenta lo que puede ser transitorio: cualquier error de
    transporte (timeout, conexión cortada, etc. — httpx.TransportError cubre
    todos esos casos), 5xx y 429. El resto de los 4xx (ej. 400 por timestamp
    desalineado) es un error del llamador, no algo que un reintento arregle,
    y se propaga de inmediato.
    """
    client = _get_cliente()
    for intento in range(REINTENTOS):
        ultimo_intento = intento == REINTENTOS - 1
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            reintentable = status >= 500 or status == 429
            if not reintentable or ultimo_intento:
                raise
        except httpx.TransportError:
            if ultimo_intento:
                raise
        await asyncio.sleep(BACKOFF_BASE_S * (2**intento))
    raise RuntimeError(
        f"_get() no hizo ningún intento: REINTENTOS={REINTENTOS} debe ser >= 1"
    )


async def obtener_champs(sport_id: int, desde: int, hasta: int) -> list[dict]:
    """Ligas con resultados en la ventana (ESPECIFICACION_FUENTE §3.1, v2)."""
    desde = alinear_ts(desde)
    hasta = alinear_ts(hasta)
    _validar_ventana(desde, hasta)
    data = await _get(
        f"{BASE_URL}/service-api/result/web/api/v2/champs",
        {
            "dateFrom": desde,
            "dateTo": hasta,
            "lng": LNG,
            "ref": REF,
            "sportIds": sport_id,
        },
    )
    return data.get("items", [])


async def obtener_partidos(champ_id: int, desde: int, hasta: int) -> list[dict]:
    """Partidos de una liga en la ventana (ESPECIFICACION_FUENTE §3.2, v3).

    Cuando `count` es 0, la clave `items` no existe en la respuesta: se usa
    siempre `data.get("items", [])`, nunca `data["items"]`.
    """
    desde = alinear_ts(desde)
    hasta = alinear_ts(hasta)
    _validar_ventana(desde, hasta)
    data = await _get(
        f"{BASE_URL}/service-api/result/web/api/v3/games",
        {"champId": champ_id, "dateFrom": desde, "dateTo": hasta, "lng": LNG, "ref": REF},
    )
    return data.get("items", [])


def es_partido_real(item: dict) -> bool:
    """Filtra agregados de liga y combinados (ESPECIFICACION_FUENTE §5).

    Hacen falta los dos filtros: los combinados tienen `dopInfo` vacío, así
    que filtrar solo por `dopInfo` los deja pasar.
    """
    if len(item.get("opp1Ids", [])) != 1:
        return False
    if len(item.get("opp2Ids", [])) != 1:
        return False
    if item.get("dopInfo"):
        return False
    return True


def parse_score(raw: str | None) -> dict | None:
    """Devuelve None si el formato no se reconoce (ESPECIFICACION_FUENTE §6).

    None nunca debe tratarse como 0:0.
    """
    if not raw:
        return None
    m = _RX_SCORE.match(raw.strip())
    if not m:
        return None
    return {
        "local": int(m.group(1)),
        "visitante": int(m.group(2)),
        "periodos_raw": m.group(3) or "",
    }


@dataclass(frozen=True)
class MercadoCuota:
    tipo: int  # T
    cuota_milesimas: int  # C, *1000 redondeado HALF_UP
    parametro: Decimal | None  # P, si viene
    group: int | None  # G


@dataclass(frozen=True)
class EventoCuotas:
    game_id: int  # I
    champ_id: int  # LI
    sport_id: int  # SI
    inicio_ts: int  # S
    equipo_local: str  # O1
    equipo_visitante: str  # O2
    mercados: list[MercadoCuota]  # E[], ya filtrados


def _parsear_mercado(e: dict) -> MercadoCuota | None:
    """None si el mercado se descarta: tipo no soportado o línea inválida
    (regla de producto, ESPECIFICACION_FUENTE §8.2 / REGLAS_LIQUIDACION §1)."""
    tipo = e["T"]
    if tipo not in TIPOS_SOPORTADOS:
        return None
    parametro = Decimal(str(e["P"])) if "P" in e else None
    if not linea_valida(parametro):
        return None
    cuota_milesimas = int((Decimal(str(e["C"])) * 1000).to_integral_value(ROUND_HALF_UP))
    return MercadoCuota(tipo=tipo, cuota_milesimas=cuota_milesimas, parametro=parametro, group=e.get("G"))


def _parsear_evento(item: dict) -> EventoCuotas:
    mercados = [m for e in item.get("E", []) if (m := _parsear_mercado(e)) is not None]
    return EventoCuotas(
        game_id=item["I"],
        champ_id=item["LI"],
        sport_id=item["SI"],
        inicio_ts=item["S"],
        equipo_local=item["O1"],
        equipo_visitante=item["O2"],
        mercados=mercados,
    )


async def obtener_cuotas(sport_id: int, count: int) -> list[EventoCuotas]:
    """Cuotas prepartido en bloque (ESPECIFICACION_FUENTE §3.3).

    ⚠️ `virtualSports=false` SIEMPRE: `true` mete deportes simulados al feed.

    LineFeed/*Zip usa el sobre `{Success, Value}` (con V mayúscula), a
    diferencia de los endpoints `/result/`, que usan `{items}`. Si
    `Success` es false, es un error — no se intenta parsear `Value`.

    Filtra mercados no soportados (REGLAS_LIQUIDACION §5) y líneas que no
    son múltiplo de 0.5 (cuartos, rechazados en Fase 1): el feed trae
    cientos de tipos, los que no sabemos liquidar no se ofrecen.
    """
    payload = await _get(
        f"{BASE_URL}/service-api/LineFeed/Get1x2_VZip",
        {
            "sports": sport_id,  # SIEMPRE filtrado por deporte: reduce a la mitad tiempo/volumen
            "count": count,
            "lng": LNG,
            "cfview": LINEFEED_CFVIEW,
            "mode": LINEFEED_MODE,
            "country": LINEFEED_COUNTRY,
            "partner": REF,
            "gr": LINEFEED_GR,
            "virtualSports": False,
        },
    )
    if not payload.get("Success"):
        raise RuntimeError(f"Get1x2_VZip devolvió Success=false: {payload.get('Error')!r}")
    return [_parsear_evento(item) for item in payload.get("Value", [])]
