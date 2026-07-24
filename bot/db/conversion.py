from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("100")
MIL = Decimal("1000")


def a_centavos(monto: Decimal) -> int:
    return int((monto * CENT).to_integral_value(ROUND_HALF_UP))


def a_decimal(centavos: int) -> Decimal:
    return Decimal(centavos) / CENT


def a_milesimas(cuota: Decimal) -> int:
    return int((cuota * MIL).to_integral_value(ROUND_HALF_UP))


def de_milesimas(milesimas: int) -> Decimal:
    return Decimal(milesimas) / MIL
