"""Punto de entrada del proceso: ensamblaje de arranque, jobs de fondo y
apagado limpio. No hay lógica de negocio nueva acá — solo conecta lo que ya
existe (`cache_cuotas`, `settlement_engine`, `cliente_1x`, `bot.db.migrar`).

Alcance: NO arranca la aplicación de Telegram. `bot/telegram/` sigue siendo
un esqueleto (CAPA 3 no implementada); este trabajo es solo la orquestación
de datos y liquidación de fondo.

⚠️ Sin conexión SQLite compartida entre operaciones. `sqlite3.Connection` no
es seguro entre corrutinas concurrentes: `liquidar_pendientes` hace `await`
a mitad de su trabajo (`cascada_fuentes.evaluar` es async y puede devolver el
control al loop con una transacción abierta) y otra corrutina usando la
MISMA conexión en ese punto corrompería su estado de transacción. WAL
permite múltiples conexiones al mismo archivo sin ese riesgo, así que cada
operación (la migración de arranque, cada corrida del job de liquidación)
abre su propia conexión y la cierra al terminar. El job de refresco de
cuotas ni siquiera toca SQLite: `cache_cuotas` vive en memoria.

⚠️ AsyncIOScheduler, NO BackgroundScheduler: el cliente httpx compartido de
`cliente_1x` es un singleton perezoso atado al event loop que lo crea
(`cliente_1x.py`); un scheduler en hilos con su propio loop rompe ese
singleton. AsyncIOScheduler corre los jobs en el mismo loop del proceso.
"""

import asyncio
import logging
import os
import signal
import sqlite3
import time
from dataclasses import dataclass

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.core import settlement_engine
from bot.db.migrar import aplicar_migraciones
from bot.fuente_resultados import cache_cuotas, cascada_fuentes
from bot.fuente_resultados.primaria import cliente_1x

_logger = logging.getLogger(__name__)


_URL_RAFAGA = (  # === DIAGNOSTICO TEMPORAL - QUITAR ===
    "https://provider.betfantasy.bet/service-api/LineFeed/Get1x2_VZip"
    "?sports=1&champs=1413697&count=50&lng=es&mode=4&country=71&partner=188"
    "&virtualSports=true&getEmpty=true&countryFirst=true"
)
_URL_ORDEN_GETEMPTY_PRIMERO = (
    "https://provider.betfantasy.bet/service-api/LineFeed/Get1x2_VZip"
    "?sports=1&champs=1146817&count=50&lng=es&mode=4&country=71&partner=188"
    "&getEmpty=true&virtualSports=true&countryFirst=true"
)
_URL_ORDEN_VIRTUALSPORTS_PRIMERO = (
    "https://provider.betfantasy.bet/service-api/LineFeed/Get1x2_VZip"
    "?sports=1&champs=1146817&count=50&lng=es&mode=4&country=71&partner=188"
    "&virtualSports=true&getEmpty=true&countryFirst=true"
)


async def _diagnostico_temporal() -> None:  # === DIAGNOSTICO TEMPORAL - QUITAR ===
    """Aísla dos hipótesis del 0% de éxito visto en el refresco real (55
    ligas, 842/842 en 502) contra el 200 de un diagnóstico aislado:
    A) orden de query params (getEmpty/virtualSports invertidos respecto del
       cliente real);
    B) rate-limiting por ráfaga (el refresco martilla ~55 ligas seguidas con
       reintentos) — y si el límite es por conexión (keep-alive) o por
       IP/volumen (conexión nueva también falla).
    Temporal: se revierte apenas se lean los logs de Railway."""
    import httpx

    # B) ráfaga con keep-alive: 25 pedidos seguidos, mismo AsyncClient
    async with httpx.AsyncClient(timeout=20.0) as c:
        for i in range(25):
            try:
                r = await c.get(_URL_RAFAGA)
                _logger.warning(
                    "DIAGNOSTICO rafaga_keepalive #%d: status=%s body[:120]=%r",
                    i + 1,
                    r.status_code,
                    r.text[:120],
                )
            except Exception:
                _logger.warning("DIAGNOSTICO rafaga_keepalive #%d: excepción", i + 1, exc_info=True)
            await asyncio.sleep(1)

    # B) ráfaga con conexión nueva cada vez: 25 pedidos, un AsyncClient por petición
    for i in range(25):
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.get(_URL_RAFAGA)
                _logger.warning(
                    "DIAGNOSTICO rafaga_conexion_nueva #%d: status=%s body[:120]=%r",
                    i + 1,
                    r.status_code,
                    r.text[:120],
                )
        except Exception:
            _logger.warning("DIAGNOSTICO rafaga_conexion_nueva #%d: excepción", i + 1, exc_info=True)
        await asyncio.sleep(1)

    # A) orden de params, misma liga (1146817, falla en producción), dos variantes
    async with httpx.AsyncClient(timeout=20.0) as c:
        for nombre, url in (
            ("getEmpty_primero", _URL_ORDEN_GETEMPTY_PRIMERO),
            ("virtualSports_primero", _URL_ORDEN_VIRTUALSPORTS_PRIMERO),
        ):
            try:
                r = await c.get(url)
                _logger.warning(
                    "DIAGNOSTICO orden(%s): status=%s body[:120]=%r",
                    nombre,
                    r.status_code,
                    r.text[:120],
                )
            except Exception:
                _logger.warning("DIAGNOSTICO orden(%s): excepción", nombre, exc_info=True)


@dataclass(frozen=True)
class Config:
    db_path: str
    sport_ids: list[int]
    intervalo_refresco_s: int
    intervalo_liquidacion_s: int
    count_cuotas: int


def cargar_config() -> Config:
    """Todo lo parametrizable sale de variables de entorno, nunca
    hardcodeado. La ruta de la DB en Railway es un volumen montado (/data),
    distinta de la de desarrollo — la pisa `BOT_DB_PATH`, no este código."""
    sport_ids_raw = os.environ.get("BOT_SPORT_IDS", str(cascada_fuentes.DEPORTE_FUTBOL))
    return Config(
        db_path=os.environ.get("BOT_DB_PATH", "betcuba.db"),
        sport_ids=[int(s) for s in sport_ids_raw.split(",")],
        # Default = la constante ya establecida en cache_cuotas, no un
        # número nuevo inventado acá.
        intervalo_refresco_s=int(os.environ.get("BOT_INTERVALO_REFRESCO_S", cache_cuotas.INTERVALO_REFRESCO_S)),
        # 3 min: cascada_fuentes ya exige 2h desde el inicio del partido y
        # 15 min de estabilidad de marcador antes de poder confirmar nada,
        # así que revisar más seguido que eso no adelanta ningún pago. 2-5
        # min es frecuente para no demorar la liquidación sin golpear la DB
        # ni el feed de más.
        intervalo_liquidacion_s=int(os.environ.get("BOT_INTERVALO_LIQUIDACION_S", 180)),
        # 50, no 1000: con count alto provider no arma el feed dentro del
        # timeout y Cloudflare corta con 502 (ESPECIFICACION_FUENTE §0). La
        # app real de betfantasy usa counts mucho menores.
        count_cuotas=int(os.environ.get("BOT_COUNT_CUOTAS", 50)),
    )


def _abrir_conexion(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")  # per-conexión: no persiste como WAL
    return conn


async def _job_refrescar_cuotas(sport_ids: list[int], count: int) -> None:
    """Un fallo de red no puede matar el job para siempre: se loguea y se
    reintenta en el próximo ciclo, nunca se propaga."""
    try:
        ahora_ts = int(time.time())
        for sport_id in sport_ids:
            await cache_cuotas.refrescar(sport_id, count, ahora_ts)
    except Exception:
        _logger.exception("refrescar_cuotas: fallo un ciclo, se reintenta en el próximo")


async def _job_liquidar_pendientes(db_path: str) -> None:
    """Abre y cierra su propia conexión (ver docstring del módulo). Igual
    que el job de refresco, un fallo se loguea y no se propaga."""
    conn = _abrir_conexion(db_path)
    try:
        ahora_ts = int(time.time())
        await settlement_engine.liquidar_pendientes(ahora_ts, conn)
    except Exception:
        _logger.exception("liquidar_pendientes: fallo un ciclo, se reintenta en el próximo")
    finally:
        conn.close()


def construir_scheduler(config: Config) -> AsyncIOScheduler:
    """`max_instances=1`: si una corrida tarda más que su intervalo, no
    arranca una segunda corrida superpuesta del mismo job. `coalesce=True`:
    si el proceso se queda sin CPU y se "pierden" varios disparos, al volver
    corre UNA sola vez, no una ráfaga de corridas atrasadas en cadena."""
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _job_refrescar_cuotas,
        "interval",
        seconds=config.intervalo_refresco_s,
        args=[config.sport_ids, config.count_cuotas],
        id="refrescar_cuotas",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _job_liquidar_pendientes,
        "interval",
        seconds=config.intervalo_liquidacion_s,
        args=[config.db_path],
        id="liquidar_pendientes",
        max_instances=1,
        coalesce=True,
    )
    return scheduler


async def main_async(config: Config, detener: asyncio.Event | None = None) -> None:
    """`ARRANQUE_TS` de `settlement_engine` (su guarda de arranque) se
    captura al importar ese módulo, arriba de este archivo — antes de que
    corra cualquier job, así que refleja el inicio real del proceso sin
    necesidad de pasarlo explícito.

    `detener`: si se pasa (tests), se usa tal cual en vez de instalar
    handlers de señal — permite disparar el apagado sin tocar señales de
    verdad."""
    conn_arranque = _abrir_conexion(config.db_path)
    aplicar_migraciones(conn_arranque)
    conn_arranque.close()

    try:  # === DIAGNOSTICO TEMPORAL - QUITAR ===
        await _diagnostico_temporal()
    except Exception:
        _logger.warning("DIAGNOSTICO: fallo inesperado al correr el diagnóstico", exc_info=True)

    scheduler = construir_scheduler(config)
    scheduler.start()

    if detener is None:
        detener = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, detener.set)

    await detener.wait()

    # wait=True: preferimos esperar a que termine un job en curso antes de
    # cerrar el cliente httpx compartido, en vez de cortar a mitad de una
    # petición o una transacción.
    scheduler.shutdown(wait=True)
    await cliente_1x.cerrar_cliente()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config = cargar_config()
    asyncio.run(main_async(config))


if __name__ == "__main__":
    main()
