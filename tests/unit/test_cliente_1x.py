import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from bot.fuente_resultados.primaria import cliente_1x

FIXTURES = Path(__file__).parents[1] / "fixtures"


def _cargar(nombre):
    return json.loads((FIXTURES / nombre).read_text())


@pytest.fixture(autouse=True)
def _resetear_cliente_compartido():
    """El cliente HTTP es un singleton perezoso ligado al event loop que lo
    creó. Cada test corre en su propio asyncio.run() (su propio event loop),
    así que hay que resetearlo entre casos o un test contamina al siguiente
    con un cliente atado a un loop ya cerrado."""
    cliente_1x._cliente = None
    yield
    cliente_1x._cliente = None


# --- alinear_ts ---------------------------------------------------------


def test_alinear_ts_ya_alineado():
    assert cliente_1x.alinear_ts(1784791200) == 1784791200
    assert cliente_1x.alinear_ts(1784880000) == 1784880000


def test_alinear_ts_no_alineado():
    assert cliente_1x.alinear_ts(1784803600) == 1784803500
    assert cliente_1x.alinear_ts(1784890000) == 1784889900


# --- es_partido_real -----------------------------------------------------


@pytest.fixture
def items_champ_2664249():
    return _cargar("partidos_champ_2664249.json")["items"]


def test_es_partido_real_partido_real(items_champ_2664249):
    item = next(i for i in items_champ_2664249 if i["_caso"] == "partido_real")
    assert cliente_1x.es_partido_real(item) is True


def test_es_partido_real_agregado_de_liga(items_champ_2664249):
    item = next(i for i in items_champ_2664249 if i["_caso"] == "agregado_de_liga")
    assert cliente_1x.es_partido_real(item) is False


def test_es_partido_real_combinado_2_equipos(items_champ_2664249):
    item = next(i for i in items_champ_2664249 if i["_caso"] == "combinado_2_equipos")
    assert cliente_1x.es_partido_real(item) is False


def test_es_partido_real_combinado_3_equipos(items_champ_2664249):
    item = next(i for i in items_champ_2664249 if i["_caso"] == "combinado_3_equipos")
    assert cliente_1x.es_partido_real(item) is False


# --- parse_score -----------------------------------------------------


@pytest.mark.parametrize(
    "raw,local,visitante,periodos_raw",
    [
        ("1:1 (1:1,0:0)", 1, 1, "1:1,0:0"),
        ("5:5(3:1,2:4)", 5, 5, "3:1,2:4"),
        ("2:2(1:0,1:2)", 2, 2, "1:0,1:2"),
        ("0:0 (0:0,0:0)", 0, 0, "0:0,0:0"),
    ],
)
def test_parse_score_formatos_validos(raw, local, visitante, periodos_raw):
    resultado = cliente_1x.parse_score(raw)
    assert resultado == {
        "local": local,
        "visitante": visitante,
        "periodos_raw": periodos_raw,
    }


@pytest.mark.parametrize("raw", ["texto raro", "", None, "2-1"])
def test_parse_score_entrada_basura_devuelve_none(raw):
    assert cliente_1x.parse_score(raw) is None


# --- obtener_partidos (mockeado a nivel de _get, ningún test toca la red) --


def test_obtener_partidos_count_cero_sin_items():
    respuesta = _cargar("partidos_vacio.json")
    assert "items" not in respuesta  # confirma la premisa del fixture

    with patch.object(cliente_1x, "_get", return_value=respuesta) as mock_get:
        resultado = asyncio.run(
            cliente_1x.obtener_partidos(champ_id=2664249, desde=1784792100, hasta=1784878500)
        )

    assert resultado == []
    mock_get.assert_called_once()


def test_obtener_partidos_con_items():
    respuesta = _cargar("partidos_champ_2664249.json")

    with patch.object(cliente_1x, "_get", return_value=respuesta):
        resultado = asyncio.run(
            cliente_1x.obtener_partidos(champ_id=2664249, desde=1784792100, hasta=1784878500)
        )

    assert len(resultado) == 4
    assert resultado[0]["id"] == 738767054


def test_obtener_partidos_alinea_timestamps_antes_de_pedir():
    respuesta = {"count": 0}
    with patch.object(cliente_1x, "_get", return_value=respuesta) as mock_get:
        asyncio.run(cliente_1x.obtener_partidos(champ_id=1, desde=1784803600, hasta=1784890000))

    _, params = mock_get.call_args[0]
    assert params["dateFrom"] == 1784803500
    assert params["dateTo"] == 1784889900


def test_obtener_partidos_rechaza_ventana_mayor_a_86400s():
    with pytest.raises(ValueError):
        asyncio.run(cliente_1x.obtener_partidos(champ_id=1, desde=0, hasta=200_000))


# --- _get: reintentos discriminados por tipo de error, sin tocar la red ---


def test_get_cliente_reutiliza_la_misma_instancia():
    a = cliente_1x._get_cliente()
    b = cliente_1x._get_cliente()
    assert a is b


def test_cerrar_cliente_resetea_el_singleton():
    cliente_1x._get_cliente()
    assert cliente_1x._cliente is not None

    asyncio.run(cliente_1x.cerrar_cliente())

    assert cliente_1x._cliente is None


def _fake_get_con_status(status_code, llamadas):
    async def fake_get(self, url, params=None, **kwargs):
        llamadas.append(status_code)
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(status_code, request=request)

    return fake_get


def test_get_400_hace_exactamente_una_peticion(monkeypatch):
    monkeypatch.setattr(cliente_1x, "BACKOFF_BASE_S", 0.001)
    llamadas = []

    with patch.object(httpx.AsyncClient, "get", _fake_get_con_status(400, llamadas)):
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(cliente_1x._get("http://example.test/x", {}))

    assert len(llamadas) == 1


def test_get_500_reintenta_hasta_agotar_intentos(monkeypatch):
    monkeypatch.setattr(cliente_1x, "BACKOFF_BASE_S", 0.001)
    llamadas = []

    with patch.object(httpx.AsyncClient, "get", _fake_get_con_status(500, llamadas)):
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(cliente_1x._get("http://example.test/x", {}))

    assert len(llamadas) == cliente_1x.REINTENTOS


def test_get_con_reintentos_cero_levanta_runtime_error_no_none(monkeypatch):
    """REINTENTOS <= 0 es una config rota: debe fallar fuerte y cerca de la
    causa, no devolver None en silencio."""
    monkeypatch.setattr(cliente_1x, "REINTENTOS", 0)

    with pytest.raises(RuntimeError):
        resultado = asyncio.run(cliente_1x._get("http://example.test/x", {}))
        assert resultado is not None  # nunca debería llegar a evaluarse
