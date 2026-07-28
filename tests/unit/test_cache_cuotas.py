import asyncio
from decimal import Decimal
from unittest.mock import patch

import pytest

from bot.fuente_resultados import cache_cuotas
from bot.fuente_resultados.primaria.cliente_1x import EventoCuotas, LigaConCuotas, MercadoCuota


@pytest.fixture(autouse=True)
def _resetear_snapshot():
    cache_cuotas._snapshot = None
    yield
    cache_cuotas._snapshot = None


@pytest.fixture(autouse=True)
def _mockear_una_liga():
    """refrescar() ahora es de dos pasos: primero lista ligas, después pide
    cuotas por cada una. Default de una sola liga para que los tests que ya
    mockean obtener_cuotas directamente sigan viendo una única llamada, tal
    como antes del flujo de dos pasos."""
    with patch.object(cache_cuotas.cliente_1x, "obtener_ligas_con_cuotas", return_value=[LigaConCuotas(champ_id=110163)]):
        yield


def _evento(game_id=730330581, champ_id=110163, sport_id=1, mercados=None):
    return EventoCuotas(
        game_id=game_id,
        champ_id=champ_id,
        sport_id=sport_id,
        inicio_ts=1787416200,
        equipo_local="Inter de Milán",
        equipo_visitante="Monza 1912",
        mercados=mercados or [],
    )


def _mercado(tipo, cuota_milesimas, parametro=None, group=None):
    return MercadoCuota(tipo=tipo, cuota_milesimas=cuota_milesimas, parametro=parametro, group=group)


# --- refrescar puebla; lectura básica ---------------------------------------


def test_refrescar_puebla_y_obtener_cuota_fresca_la_devuelve():
    evento = _evento(mercados=[_mercado(1, 1264)])

    with patch.object(cache_cuotas.cliente_1x, "obtener_cuotas", return_value=[evento]):
        asyncio.run(cache_cuotas.refrescar(sport_id=1, count=1000, ahora_ts=1000))

    cuota = cache_cuotas.obtener_cuota_fresca(730330581, 1, None, ahora_ts=1010)
    assert cuota is not None
    assert cuota.cuota_milesimas == 1264
    assert cuota.game_id == 730330581
    assert cuota.champ_id == 110163
    assert cuota.capturada_ts == 1000


# --- TTL ---------------------------------------------------------------------


def test_dentro_del_ttl_es_fresca():
    evento = _evento(mercados=[_mercado(1, 1264)])
    with patch.object(cache_cuotas.cliente_1x, "obtener_cuotas", return_value=[evento]):
        asyncio.run(cache_cuotas.refrescar(sport_id=1, count=1000, ahora_ts=1000))

    cuota = cache_cuotas.obtener_cuota_fresca(730330581, 1, None, ahora_ts=1000 + cache_cuotas.TTL_S)
    assert cuota is not None


def test_pasado_el_ttl_devuelve_none():
    evento = _evento(mercados=[_mercado(1, 1264)])
    with patch.object(cache_cuotas.cliente_1x, "obtener_cuotas", return_value=[evento]):
        asyncio.run(cache_cuotas.refrescar(sport_id=1, count=1000, ahora_ts=1000))

    cuota = cache_cuotas.obtener_cuota_fresca(730330581, 1, None, ahora_ts=1000 + cache_cuotas.TTL_S + 1)
    assert cuota is None


# --- mercado ausente -----------------------------------------------------


def test_mercado_ausente_devuelve_none():
    evento = _evento(mercados=[_mercado(1, 1264)])
    with patch.object(cache_cuotas.cliente_1x, "obtener_cuotas", return_value=[evento]):
        asyncio.run(cache_cuotas.refrescar(sport_id=1, count=1000, ahora_ts=1000))

    assert cache_cuotas.obtener_cuota_fresca(730330581, 9, Decimal("2.5"), ahora_ts=1000) is None


def test_sin_refrescar_nunca_devuelve_none():
    assert cache_cuotas.obtener_cuota_fresca(1, 1, None, ahora_ts=1000) is None


# --- parametro distinto = clave distinta ------------------------------------


def test_parametro_distinto_no_se_pisan():
    evento = _evento(
        mercados=[
            _mercado(9, 1708, parametro=Decimal("2.5")),
            _mercado(9, 2500, parametro=Decimal("3.5")),
        ]
    )
    with patch.object(cache_cuotas.cliente_1x, "obtener_cuotas", return_value=[evento]):
        asyncio.run(cache_cuotas.refrescar(sport_id=1, count=1000, ahora_ts=1000))

    cuota_2_5 = cache_cuotas.obtener_cuota_fresca(730330581, 9, Decimal("2.5"), ahora_ts=1000)
    cuota_3_5 = cache_cuotas.obtener_cuota_fresca(730330581, 9, Decimal("3.5"), ahora_ts=1000)
    assert cuota_2_5.cuota_milesimas == 1708
    assert cuota_3_5.cuota_milesimas == 2500


# --- refresco que falla conserva el snapshot --------------------------------


def test_refresco_fallido_conserva_snapshot_y_no_rejuvenece():
    evento = _evento(mercados=[_mercado(1, 1264)])
    with patch.object(cache_cuotas.cliente_1x, "obtener_cuotas", return_value=[evento]):
        asyncio.run(cache_cuotas.refrescar(sport_id=1, count=1000, ahora_ts=1000))

    with patch.object(cache_cuotas.cliente_1x, "obtener_cuotas", side_effect=RuntimeError("feed caído")):
        asyncio.run(cache_cuotas.refrescar(sport_id=1, count=1000, ahora_ts=1020))

    # Sigue estando la cuota original, capturada en ts=1000, no en ts=1020.
    cuota = cache_cuotas.obtener_cuota_fresca(730330581, 1, None, ahora_ts=1020)
    assert cuota is not None
    assert cuota.capturada_ts == 1000

    # Y sigue venciendo según ese ts=1000 original, no según el intento fallido.
    vencida = cache_cuotas.obtener_cuota_fresca(730330581, 1, None, ahora_ts=1000 + cache_cuotas.TTL_S + 1)
    assert vencida is None


# --- feed caído más que el TTL: todo vence ----------------------------------


def test_feed_caido_mas_que_ttl_vence_todo():
    evento = _evento(
        mercados=[_mercado(1, 1264), _mercado(2, 6670), _mercado(9, 1708, parametro=Decimal("2.5"))]
    )
    with patch.object(cache_cuotas.cliente_1x, "obtener_cuotas", return_value=[evento]):
        asyncio.run(cache_cuotas.refrescar(sport_id=1, count=1000, ahora_ts=1000))

    with patch.object(cache_cuotas.cliente_1x, "obtener_cuotas", side_effect=RuntimeError("feed caído")):
        for intento_ts in (1015, 1030, 1045):
            asyncio.run(cache_cuotas.refrescar(sport_id=1, count=1000, ahora_ts=intento_ts))

    ahora_ts = 1000 + cache_cuotas.TTL_S + 1
    assert cache_cuotas.obtener_cuota_fresca(730330581, 1, None, ahora_ts) is None
    assert cache_cuotas.obtener_cuota_fresca(730330581, 2, None, ahora_ts) is None
    assert cache_cuotas.obtener_cuota_fresca(730330581, 9, Decimal("2.5"), ahora_ts) is None


# --- flujo de dos pasos: listar ligas, después pedir cuotas por cada una ---


def test_refrescar_combina_eventos_de_varias_ligas():
    evento_a = _evento(game_id=1, champ_id=100, mercados=[_mercado(1, 1500)])
    evento_b = _evento(game_id=2, champ_id=200, mercados=[_mercado(1, 2000)])
    ligas = [LigaConCuotas(champ_id=100), LigaConCuotas(champ_id=200)]

    with patch.object(cache_cuotas.cliente_1x, "obtener_ligas_con_cuotas", return_value=ligas), patch.object(
        cache_cuotas.cliente_1x, "obtener_cuotas", side_effect=[[evento_a], [evento_b]]
    ) as mock_obtener_cuotas:
        asyncio.run(cache_cuotas.refrescar(sport_id=1, count=1000, ahora_ts=1000))

    assert mock_obtener_cuotas.call_count == 2
    mock_obtener_cuotas.assert_any_call(1, 100, 1000)
    mock_obtener_cuotas.assert_any_call(1, 200, 1000)
    assert cache_cuotas.obtener_cuota_fresca(1, 1, None, ahora_ts=1000) is not None
    assert cache_cuotas.obtener_cuota_fresca(2, 1, None, ahora_ts=1000) is not None


def test_refrescar_si_falla_listar_ligas_conserva_snapshot():
    evento = _evento(mercados=[_mercado(1, 1264)])
    with patch.object(cache_cuotas.cliente_1x, "obtener_cuotas", return_value=[evento]):
        asyncio.run(cache_cuotas.refrescar(sport_id=1, count=1000, ahora_ts=1000))

    with patch.object(cache_cuotas.cliente_1x, "obtener_ligas_con_cuotas", side_effect=RuntimeError("caído")):
        asyncio.run(cache_cuotas.refrescar(sport_id=1, count=1000, ahora_ts=1020))

    cuota = cache_cuotas.obtener_cuota_fresca(730330581, 1, None, ahora_ts=1020)
    assert cuota is not None
    assert cuota.capturada_ts == 1000  # no se tocó: el snapshot viejo sigue entero


def test_refrescar_si_falla_una_liga_entre_varias_salta_y_sigue_con_las_demas():
    """Mejor esfuerzo en el paso 2 (CACHE_CUOTAS §7): una liga puede haber
    pasado a en vivo entre el paso 1 y el paso 2, o no tener feed
    prepartido en este momento. Se salta esa liga y se sigue con las
    demás — no se aborta el refresco entero por una sola liga caída."""
    evento_a = _evento(game_id=1, champ_id=100, mercados=[_mercado(1, 1500)])
    evento_c = _evento(game_id=3, champ_id=300, mercados=[_mercado(1, 1700)])
    ligas = [LigaConCuotas(champ_id=100), LigaConCuotas(champ_id=200), LigaConCuotas(champ_id=300)]

    with patch.object(cache_cuotas.cliente_1x, "obtener_ligas_con_cuotas", return_value=ligas), patch.object(
        cache_cuotas.cliente_1x,
        "obtener_cuotas",
        side_effect=[[evento_a], RuntimeError("liga 200 en vivo, 502"), [evento_c]],
    ):
        asyncio.run(cache_cuotas.refrescar(sport_id=1, count=1000, ahora_ts=1000))

    # Las dos ligas buenas (100 y 300) quedaron en el snapshot nuevo.
    assert cache_cuotas.obtener_cuota_fresca(1, 1, None, ahora_ts=1000) is not None
    assert cache_cuotas.obtener_cuota_fresca(3, 1, None, ahora_ts=1000) is not None


def test_refrescar_si_todas_las_ligas_fallan_conserva_snapshot_anterior():
    """Guarda de seguridad: si TODAS las ligas del paso 2 fallan, no se
    reemplaza el snapshot por uno vacío — un snapshot vacío dejaría al bot
    sin cuotas cuando el problema puede ser transitorio de esta corrida."""
    evento_previo = _evento(game_id=9, champ_id=900, mercados=[_mercado(1, 1111)])
    with patch.object(
        cache_cuotas.cliente_1x, "obtener_ligas_con_cuotas", return_value=[LigaConCuotas(champ_id=900)]
    ), patch.object(cache_cuotas.cliente_1x, "obtener_cuotas", return_value=[evento_previo]):
        asyncio.run(cache_cuotas.refrescar(sport_id=1, count=1000, ahora_ts=1000))

    ligas = [LigaConCuotas(champ_id=100), LigaConCuotas(champ_id=200)]
    with patch.object(cache_cuotas.cliente_1x, "obtener_ligas_con_cuotas", return_value=ligas), patch.object(
        cache_cuotas.cliente_1x,
        "obtener_cuotas",
        side_effect=[RuntimeError("liga 100 caída"), RuntimeError("liga 200 caída")],
    ):
        asyncio.run(cache_cuotas.refrescar(sport_id=1, count=1000, ahora_ts=1020))

    # El snapshot sigue siendo el previo (game_id=9): ni rastro de las ligas nuevas.
    cuota = cache_cuotas.obtener_cuota_fresca(9, 1, None, ahora_ts=1020)
    assert cuota is not None
    assert cuota.capturada_ts == 1000


def test_refrescar_paso_1_devuelve_cero_ligas_conserva_snapshot_anterior():
    """El paso 1 devolvió una lista vacía SIN excepción — se trata igual
    que 'todas fallan', se conserva el snapshot anterior. Desde `Value: []`
    no se puede distinguir "no hay fútbol ahora" de un hueco momentáneo del
    listado; vaciar activamente rechazaría toda apuesta de inmediato. El
    TTL (§6) es quien vence las cuotas viejas si de verdad no hay ligas."""
    evento_previo = _evento(game_id=9, champ_id=900, mercados=[_mercado(1, 1111)])
    with patch.object(
        cache_cuotas.cliente_1x, "obtener_ligas_con_cuotas", return_value=[LigaConCuotas(champ_id=900)]
    ), patch.object(cache_cuotas.cliente_1x, "obtener_cuotas", return_value=[evento_previo]):
        asyncio.run(cache_cuotas.refrescar(sport_id=1, count=1000, ahora_ts=1000))

    with patch.object(cache_cuotas.cliente_1x, "obtener_ligas_con_cuotas", return_value=[]):
        asyncio.run(cache_cuotas.refrescar(sport_id=1, count=1000, ahora_ts=1020))

    # El snapshot sigue siendo el previo: la cuota original sigue ahí.
    cuota = cache_cuotas.obtener_cuota_fresca(9, 1, None, ahora_ts=1020)
    assert cuota is not None
    assert cuota.capturada_ts == 1000
