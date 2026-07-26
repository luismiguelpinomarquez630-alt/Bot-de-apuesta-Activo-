import asyncio
import json
from decimal import Decimal
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


def _fake_get_secuencia(respuestas, llamadas):
    """respuestas: lista de (status_code, cuerpo_dict_o_None). Una por
    llamada, en orden."""
    it = iter(respuestas)

    async def fake_get(self, url, params=None, **kwargs):
        status_code, cuerpo = next(it)
        llamadas.append(status_code)
        request = httpx.Request("GET", url, params=params)
        content = json.dumps(cuerpo).encode() if cuerpo is not None else b""
        return httpx.Response(status_code, request=request, content=content)

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


# --- provider_request_busy: detectado por BODY, no por status --------------


def test_get_provider_busy_reintenta_y_devuelve_la_respuesta_real(monkeypatch):
    monkeypatch.setattr(cliente_1x, "BACKOFF_BASE_S", 0.001)
    llamadas = []
    respuestas = [
        (200, {"error": "provider_request_busy"}),
        (200, {"ok": True}),
    ]

    with patch.object(httpx.AsyncClient, "get", _fake_get_secuencia(respuestas, llamadas)):
        resultado = asyncio.run(cliente_1x._get("http://example.test/x", {}))

    assert resultado == {"ok": True}
    assert len(llamadas) == 2


def test_get_provider_busy_se_detecta_con_status_409_no_reintentable_normalmente(monkeypatch):
    """provider devolvió provider_request_busy con 409 en las observaciones
    reales — 409 no es 5xx ni 429, así que sin detección por body esto
    hubiera abortado en el primer intento. Se detecta por el body, no por
    el status."""
    monkeypatch.setattr(cliente_1x, "BACKOFF_BASE_S", 0.001)
    llamadas = []
    respuestas = [
        (409, {"error": "provider_request_busy"}),
        (200, {"ok": True}),
    ]

    with patch.object(httpx.AsyncClient, "get", _fake_get_secuencia(respuestas, llamadas)):
        resultado = asyncio.run(cliente_1x._get("http://example.test/x", {}))

    assert resultado == {"ok": True}
    assert len(llamadas) == 2


def test_get_provider_busy_agota_reintentos_levanta_runtime_error(monkeypatch):
    monkeypatch.setattr(cliente_1x, "BACKOFF_BASE_S", 0.001)
    llamadas = []
    respuestas = [(200, {"error": "provider_request_busy"})] * cliente_1x.REINTENTOS

    with patch.object(httpx.AsyncClient, "get", _fake_get_secuencia(respuestas, llamadas)):
        with pytest.raises(RuntimeError):
            asyncio.run(cliente_1x._get("http://example.test/x", {}))

    assert len(llamadas) == cliente_1x.REINTENTOS


def test_get_backoff_base_es_3s():
    """3s, 6s, 12s con la fórmula existente (BACKOFF_BASE_S * 2**intento) —
    ESPECIFICACION_FUENTE §0."""
    assert cliente_1x.BACKOFF_BASE_S == 3.0


# --- obtener_ligas_con_cuotas (paso 1 del flujo de dos pasos) --------------


def test_obtener_ligas_con_cuotas_filtra_por_sport_y_mapea_champ_id():
    respuesta = {
        "Success": True,
        "Value": [
            {"LI": 110163, "SI": 1, "L": "Italia. Serie A"},
            {"LI": 2664249, "SI": 1, "L": "Australia. NPL Victoria"},
            {"LI": 999999, "SI": 3, "L": "Liga de baloncesto"},  # otro deporte, se descarta
        ],
    }

    with patch.object(cliente_1x, "_get", return_value=respuesta):
        resultado = asyncio.run(cliente_1x.obtener_ligas_con_cuotas(sport_id=1))

    assert {liga.champ_id for liga in resultado} == {110163, 2664249}


def test_obtener_ligas_con_cuotas_descarta_item_sin_li_sin_reventar():
    """provider es intermitente: un item parcial sin LI no debe tumbar el
    refresco de las demás ligas."""
    respuesta = {
        "Success": True,
        "Value": [
            {"SI": 1, "L": "Liga sin id"},  # sin LI, se descarta
            {"LI": 110163, "SI": 1, "L": "Italia. Serie A"},
        ],
    }

    with patch.object(cliente_1x, "_get", return_value=respuesta):
        resultado = asyncio.run(cliente_1x.obtener_ligas_con_cuotas(sport_id=1))

    assert {liga.champ_id for liga in resultado} == {110163}


def test_obtener_ligas_con_cuotas_success_false_levanta_error():
    respuesta = {"Success": False, "Error": "algo salió mal", "Value": []}

    with patch.object(cliente_1x, "_get", return_value=respuesta):
        with pytest.raises(RuntimeError):
            asyncio.run(cliente_1x.obtener_ligas_con_cuotas(sport_id=1))


def test_obtener_ligas_con_cuotas_value_vacio_devuelve_lista_vacia():
    respuesta = {"Success": True, "Value": []}

    with patch.object(cliente_1x, "_get", return_value=respuesta):
        resultado = asyncio.run(cliente_1x.obtener_ligas_con_cuotas(sport_id=1))

    assert resultado == []


def test_obtener_ligas_con_cuotas_pide_los_parametros_de_operador_correctos():
    respuesta = {"Success": True, "Value": []}

    with patch.object(cliente_1x, "_get", return_value=respuesta) as mock_get:
        asyncio.run(cliente_1x.obtener_ligas_con_cuotas(sport_id=1))

    url, params = mock_get.call_args[0]
    assert "WebGetTopChampsZip" in url
    assert params["country"] == 71
    assert params["partner"] == 188


# --- obtener_cuotas (mockeado a nivel de _get, ningún test toca la red) ----


def test_obtener_cuotas_filtra_tipo_no_soportado_y_su_linea():
    respuesta = _cargar("lineFeed_get1x2_vzip.json")

    with patch.object(cliente_1x, "_get", return_value=respuesta):
        resultado = asyncio.run(cliente_1x.obtener_cuotas(sport_id=1, champ_id=110163, count=1000))

    assert len(resultado) == 1
    evento = resultado[0]
    assert len(evento.mercados) == 9
    assert {m.tipo for m in evento.mercados} == {1, 2, 3, 7, 8, 9, 10, 180, 181}
    assert 3827 not in {m.tipo for m in evento.mercados}


def test_obtener_cuotas_mapea_el_evento():
    respuesta = _cargar("lineFeed_get1x2_vzip.json")

    with patch.object(cliente_1x, "_get", return_value=respuesta):
        resultado = asyncio.run(cliente_1x.obtener_cuotas(sport_id=1, champ_id=110163, count=1000))

    evento = resultado[0]
    assert evento.game_id == 730330581
    assert evento.champ_id == 110163
    assert evento.sport_id == 1
    assert evento.inicio_ts == 1787416200
    assert evento.equipo_local == "Inter de Milán"
    assert evento.equipo_visitante == "Monza 1912"


def test_obtener_cuotas_cuota_milesimas():
    respuesta = _cargar("lineFeed_get1x2_vzip.json")

    with patch.object(cliente_1x, "_get", return_value=respuesta):
        resultado = asyncio.run(cliente_1x.obtener_cuotas(sport_id=1, champ_id=110163, count=1000))

    mercado_t7 = next(m for m in resultado[0].mercados if m.tipo == 7)
    assert mercado_t7.cuota_milesimas == 1788
    assert mercado_t7.parametro == Decimal("-1.5")
    assert mercado_t7.group == 2


def test_obtener_cuotas_success_false_levanta_error():
    respuesta = {"Success": False, "Error": "algo salió mal", "Value": []}

    with patch.object(cliente_1x, "_get", return_value=respuesta):
        with pytest.raises(RuntimeError):
            asyncio.run(cliente_1x.obtener_cuotas(sport_id=1, champ_id=110163, count=1000))


def test_obtener_cuotas_value_vacio_devuelve_lista_vacia():
    respuesta = {"Success": True, "Value": []}

    with patch.object(cliente_1x, "_get", return_value=respuesta):
        resultado = asyncio.run(cliente_1x.obtener_cuotas(sport_id=1, champ_id=110163, count=1000))

    assert resultado == []


def test_obtener_cuotas_pide_champ_id_y_virtual_sports_true():
    """ESPECIFICACION_FUENTE §0: provider exige `champs=<champ_id>` (paso 2
    del flujo de dos pasos) y usa `virtualSports=true`, al revés de 1x — el
    whitelist del paso 1 ya excluye ligas sintéticas, este flag no filtra
    nada más."""
    respuesta = {"Success": True, "Value": []}

    with patch.object(cliente_1x, "_get", return_value=respuesta) as mock_get:
        asyncio.run(cliente_1x.obtener_cuotas(sport_id=1, champ_id=110163, count=1000))

    _, params = mock_get.call_args[0]
    assert params["champs"] == 110163
    assert params["virtualSports"] is True
