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

import httpx

HOST = "https://bol.1xbet.com"
REF = 156
LNG = "es"

VENTANA_MAX_S = 86400  # único tamaño de ventana verificado, ESPECIFICACION_FUENTE §2
TIMEOUT_S = 10
REINTENTOS = 3
BACKOFF_BASE_S = 1.0

_RX_SCORE = re.compile(r"^(\d+):(\d+)\s*(?:\(([^)]*)\))?$")


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

    Solo se reintenta lo que puede ser transitorio: timeout, error de
    conexión, 5xx y 429. El resto de los 4xx (ej. 400 por timestamp
    desalineado) es un error del llamador, no algo que un reintento arregle,
    y se propaga de inmediato.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
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
            except (httpx.TimeoutException, httpx.ConnectError):
                if ultimo_intento:
                    raise
            await asyncio.sleep(BACKOFF_BASE_S * (2**intento))


async def obtener_champs(sport_id: int, desde: int, hasta: int) -> list[dict]:
    """Ligas con resultados en la ventana (ESPECIFICACION_FUENTE §3.1, v2)."""
    desde = alinear_ts(desde)
    hasta = alinear_ts(hasta)
    _validar_ventana(desde, hasta)
    data = await _get(
        f"{HOST}/service-api/result/web/api/v2/champs",
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
        f"{HOST}/service-api/result/web/api/v3/games",
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
