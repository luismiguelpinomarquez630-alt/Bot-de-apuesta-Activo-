from decimal import Decimal

import pytest

from bot.dominio.mercados import (
    TIPOS_SOPORTADOS,
    centesimas_a_linea,
    linea_a_centesimas,
    linea_valida,
)


def test_tipos_soportados_son_los_16_de_reglas_liquidacion():
    assert TIPOS_SOPORTADOS == frozenset(
        {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 180, 181}
    )


def test_linea_valida_multiplos_de_0_5():
    assert linea_valida(Decimal("2.5")) is True
    assert linea_valida(Decimal("2.0")) is True
    assert linea_valida(Decimal("-1.5")) is True
    assert linea_valida(None) is True


def test_linea_valida_rechaza_cuartos():
    assert linea_valida(Decimal("2.75")) is False
    assert linea_valida(Decimal("1.25")) is False


# --- linea_a_centesimas / centesimas_a_linea --------------------------------


@pytest.mark.parametrize("p", [Decimal("-1.5"), Decimal("0.5"), Decimal("2.5"), Decimal("-0.5"), Decimal("3.5")])
def test_ida_y_vuelta_no_pierde_el_valor(p):
    assert centesimas_a_linea(linea_a_centesimas(p)) == p


def test_linea_a_centesimas_negativa():
    assert linea_a_centesimas(Decimal("-1.5")) == -150


def test_centesimas_a_linea_negativa():
    assert centesimas_a_linea(-150) == Decimal("-1.5")
