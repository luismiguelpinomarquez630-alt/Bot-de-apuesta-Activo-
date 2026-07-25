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
        count_cuotas=int(os.environ.get("BOT_COUNT_CUOTAS", 1000)),
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


# === DIAGNOSTICO TEMPORAL - QUITAR ===
async def _diagnostico_temporal() -> None:
    """Desechable: `LineFeed/Get1x2_VZip` da 502 en provider — ese endpoint
    no existe ahí. Provider sirve `LiveFeed` y `result`, pero no `LineFeed`.
    Hay que descubrir por qué endpoint sirve las cuotas. Prueba 3
    candidatos (todos con `gr=413`/`country=94`, timeout 20s) a ver cuál
    responde 200 con el sobre `{Success, Value}`. Borrar esta función y su
    llamada en main_async una vez leídos los logs de Railway.

    ⚠️ Nunca puede impedir el arranque: TODO acá adentro está cubierto por
    un try/except Exception amplio (no solo httpx.HTTPError) que loguea y
    sigue — si un host cuelga o el diagnóstico mismo tiene un bug, el
    scheduler tiene que arrancar igual.
    """
    try:
        import httpx

        urls = [
            (
                "LiveFeed Get1x2_VZip (LiveFeed en vez de LineFeed)",
                "https://provider.betfantasy.bet/service-api/LiveFeed/Get1x2_VZip"
                "?sports=1&count=10&lng=es&cfview=2&mode=4&country=94&partner=156&gr=413",
            ),
            (
                "LiveFeed GetChampZip (cuotas por liga)",
                "https://provider.betfantasy.bet/service-api/LiveFeed/GetChampZip"
                "?lng=es&gr=413&country=94&sports=1&count=10",
            ),
            (
                "LiveFeed GetTopGamesStatZip (feed de eventos de las capturas iniciales)",
                "https://provider.betfantasy.bet/service-api/LiveFeed/GetTopGamesStatZip"
                "?lng=es&gr=413&country=94&sports=1&count=10",
            ),
        ]

        async with httpx.AsyncClient(timeout=20) as client:
            try:
                ip_resp = await client.get("https://api.ipify.org")
                _logger.warning("DIAGNOSTICO: IP de salida = %s", ip_resp.text.strip())
            except Exception as exc:
                _logger.warning("DIAGNOSTICO: fallo al consultar IP de salida: %r", exc)

            for nombre, url in urls:
                inicio = time.monotonic()
                try:
                    resp = await client.get(url)
                    duracion_s = time.monotonic() - inicio
                    _logger.warning(
                        "DIAGNOSTICO: %s -> status=%s duracion=%.2fs body[:200]=%r",
                        nombre,
                        resp.status_code,
                        duracion_s,
                        resp.text[:200],
                    )
                except Exception as exc:
                    duracion_s = time.monotonic() - inicio
                    _logger.warning(
                        "DIAGNOSTICO: %s -> error de transporte tras %.2fs: %r", nombre, duracion_s, exc
                    )
    except Exception:
        # Paraguas final: cualquier fallo no anticipado (import, construcción
        # del cliente, lo que sea) se loguea acá y el arranque sigue.
        _logger.exception("DIAGNOSTICO: el bloque temporal falló entero, se ignora")
# === FIN DIAGNOSTICO TEMPORAL ===


async def main_async(config: Config, detener: asyncio.Event | None = None) -> None:
    """`ARRANQUE_TS` de `settlement_engine` (su guarda de arranque) se
    captura al importar ese módulo, arriba de este archivo — antes de que
    corra cualquier job, así que refleja el inicio real del proceso sin
    necesidad de pasarlo explícito.

    `detener`: si se pasa (tests), se usa tal cual en vez de instalar
    handlers de señal — permite disparar el apagado sin tocar señales de
    verdad."""
    # === DIAGNOSTICO TEMPORAL - QUITAR ===
    await _diagnostico_temporal()
    # === FIN DIAGNOSTICO TEMPORAL ===

    conn_arranque = _abrir_conexion(config.db_path)
    aplicar_migraciones(conn_arranque)
    conn_arranque.close()

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
