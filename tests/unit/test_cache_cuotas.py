import asyncio
from decimal import Decimal
from unittest.mock import patch

import pytest

from bot.fuente_resultados import cache_cuotas
from bot.fuente_resultados.primaria.cliente_1x import EventoCuotas, MercadoCuota


@pytest.fixture(autouse=True)
def _resetear_snapshot():
    cache_cuotas._snapshot = None
    yield
    cache_cuotas._snapshot = None


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
