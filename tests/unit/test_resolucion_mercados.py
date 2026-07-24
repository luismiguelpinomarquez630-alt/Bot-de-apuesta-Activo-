from decimal import Decimal

import pytest

from bot.core.resolucion_mercados import (
    EstadoSeleccion,
    Marcador,
    parametro_valido,
    resolver,
    tipo_soportado,
)

G = EstadoSeleccion.GANADA
P = EstadoSeleccion.PERDIDA
N = EstadoSeleccion.NULA


def _m(local, visitante):
    return Marcador(local=local, visitante=visitante, periodos_raw="")


# --- 1. Matriz completa: cada tipo contra 2:0, 0:2, 1:1, 0:0, 3:1 ----------
#
# Para los tipos con parámetro (7-14) se usa un P de medio punto (0.5) que
# nunca cae en push, para no mezclar esta matriz general con los tests de
# push explícitos de la sección 2. El 3:1 es obligatorio: junto con 2:0/0:2
# es lo único que detecta local/visitante invertidos en T7/T8/T11-T14.

CASOS_MATRIZ = [
    # --- 5.1 Resultado 1X2 — sin parámetro, nunca NULA ---
    (1, None, 2, 0, G), (1, None, 0, 2, P), (1, None, 1, 1, P), (1, None, 0, 0, P), (1, None, 3, 1, G),
    (2, None, 2, 0, P), (2, None, 0, 2, P), (2, None, 1, 1, G), (2, None, 0, 0, G), (2, None, 3, 1, P),
    (3, None, 2, 0, P), (3, None, 0, 2, G), (3, None, 1, 1, P), (3, None, 0, 0, P), (3, None, 3, 1, P),
    # --- 5.2 Doble oportunidad — sin parámetro, nunca NULA ---
    (4, None, 2, 0, G), (4, None, 0, 2, P), (4, None, 1, 1, G), (4, None, 0, 0, G), (4, None, 3, 1, G),
    (5, None, 2, 0, G), (5, None, 0, 2, G), (5, None, 1, 1, P), (5, None, 0, 0, P), (5, None, 3, 1, G),
    (6, None, 2, 0, P), (6, None, 0, 2, G), (6, None, 1, 1, G), (6, None, 0, 0, G), (6, None, 3, 1, P),
    # --- 5.3 Hándicap, P=0.5 (nunca push con línea de medio punto) ---
    (7, Decimal("0.5"), 2, 0, G), (7, Decimal("0.5"), 0, 2, P), (7, Decimal("0.5"), 1, 1, G),
    (7, Decimal("0.5"), 0, 0, G), (7, Decimal("0.5"), 3, 1, G),
    (8, Decimal("0.5"), 2, 0, P), (8, Decimal("0.5"), 0, 2, G), (8, Decimal("0.5"), 1, 1, G),
    (8, Decimal("0.5"), 0, 0, G), (8, Decimal("0.5"), 3, 1, P),
    # --- 5.4 Total de goles, P=1.5 ---
    (9, Decimal("1.5"), 2, 0, G), (9, Decimal("1.5"), 0, 2, G), (9, Decimal("1.5"), 1, 1, G),
    (9, Decimal("1.5"), 0, 0, P), (9, Decimal("1.5"), 3, 1, G),
    (10, Decimal("1.5"), 2, 0, P), (10, Decimal("1.5"), 0, 2, P), (10, Decimal("1.5"), 1, 1, P),
    (10, Decimal("1.5"), 0, 0, G), (10, Decimal("1.5"), 3, 1, P),
    # --- 5.5 Total individual, P=0.5 ---
    (11, Decimal("0.5"), 2, 0, G), (11, Decimal("0.5"), 0, 2, P), (11, Decimal("0.5"), 1, 1, G),
    (11, Decimal("0.5"), 0, 0, P), (11, Decimal("0.5"), 3, 1, G),
    (12, Decimal("0.5"), 2, 0, P), (12, Decimal("0.5"), 0, 2, G), (12, Decimal("0.5"), 1, 1, P),
    (12, Decimal("0.5"), 0, 0, G), (12, Decimal("0.5"), 3, 1, P),
    (13, Decimal("0.5"), 2, 0, P), (13, Decimal("0.5"), 0, 2, G), (13, Decimal("0.5"), 1, 1, G),
    (13, Decimal("0.5"), 0, 0, P), (13, Decimal("0.5"), 3, 1, G),
    (14, Decimal("0.5"), 2, 0, G), (14, Decimal("0.5"), 0, 2, P), (14, Decimal("0.5"), 1, 1, P),
    (14, Decimal("0.5"), 0, 0, G), (14, Decimal("0.5"), 3, 1, P),
    # --- 5.6 Ambos equipos marcan — sin parámetro, nunca NULA ---
    (180, None, 2, 0, P), (180, None, 0, 2, P), (180, None, 1, 1, G), (180, None, 0, 0, P), (180, None, 3, 1, G),
    (181, None, 2, 0, G), (181, None, 0, 2, G), (181, None, 1, 1, P), (181, None, 0, 0, G), (181, None, 3, 1, P),
]


@pytest.mark.parametrize("tipo,parametro,local,visitante,esperado", CASOS_MATRIZ)
def test_matriz_completa_por_tipo(tipo, parametro, local, visitante, esperado):
    resultado = resolver(tipo, parametro, _m(local, visitante))
    assert resultado == esperado


# --- 2. Push: T7/T8 con P entero y diferencia exacta; T9/T10 con total
#              exacto; T11..T14 con goles exactos ---------------------------


def test_push_t7_handicap_local():
    # Marcador 2:0, P=-2.0 -> (2-2)-0 = 0.0 -> NULA (ejemplo REGLAS_LIQUIDACION §5.3)
    assert resolver(7, Decimal("-2.0"), _m(2, 0)) == N


def test_push_t8_handicap_visitante():
    # Marcador 2:0, P=+2.0 -> (0+2)-2 = 0.0 -> NULA (ejemplo REGLAS_LIQUIDACION §5.3)
    assert resolver(8, Decimal("2.0"), _m(2, 0)) == N


def test_push_t9_total_mas_de():
    # Marcador 1:1 (total 2), P=2.0 -> línea exacta -> NULA
    assert resolver(9, Decimal("2.0"), _m(1, 1)) == N


def test_push_t10_total_menos_de():
    assert resolver(10, Decimal("2.0"), _m(1, 1)) == N


def test_push_t11_total_individual_local_mas_de():
    assert resolver(11, Decimal("2"), _m(2, 0)) == N


def test_push_t12_total_individual_local_menos_de():
    assert resolver(12, Decimal("2"), _m(2, 0)) == N


def test_push_t13_total_individual_visitante_mas_de():
    assert resolver(13, Decimal("2"), _m(0, 2)) == N


def test_push_t14_total_individual_visitante_menos_de():
    assert resolver(14, Decimal("2"), _m(0, 2)) == N


# --- 3. parametro_valido ----------------------------------------------------


def test_parametro_valido_multiplos_de_0_5():
    assert parametro_valido(Decimal("2.5")) is True
    assert parametro_valido(Decimal("2.0")) is True
    assert parametro_valido(None) is True


def test_parametro_valido_rechaza_cuartos():
    assert parametro_valido(Decimal("2.75")) is False
    assert parametro_valido(Decimal("1.25")) is False


# --- 4. tipo_soportado -------------------------------------------------------


def test_tipo_soportado_ambos_equipos_marcan():
    assert tipo_soportado(180) is True
    assert tipo_soportado(181) is True


def test_tipo_soportado_rechaza_no_verificados():
    assert tipo_soportado(196) is False
    assert tipo_soportado(1737) is False


# --- 5. resolver() valida entrada: ValueError, nunca REQUIERE_ADMIN --------


def test_resolver_tipo_no_soportado_levanta_value_error():
    with pytest.raises(ValueError):
        resolver(196, None, _m(1, 1))


def test_resolver_parametro_invalido_levanta_value_error():
    with pytest.raises(ValueError):
        resolver(9, Decimal("2.75"), _m(1, 1))


def test_resolver_tipo_con_parametro_sin_parametro_levanta_value_error():
    with pytest.raises(ValueError):
        resolver(9, None, _m(1, 1))


def test_resolver_tipo_sin_parametro_con_parametro_levanta_value_error():
    with pytest.raises(ValueError):
        resolver(1, Decimal("2.5"), _m(1, 1))
