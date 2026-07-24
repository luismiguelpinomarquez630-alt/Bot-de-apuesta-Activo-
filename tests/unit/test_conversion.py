from decimal import Decimal

from bot.db.conversion import a_centavos, a_decimal, a_milesimas, de_milesimas


def test_round_trip_centavos():
    for monto in (Decimal("0.01"), Decimal("20"), Decimal("500"), Decimal("1234.56")):
        assert a_decimal(a_centavos(monto)) == monto


def test_round_trip_milesimas():
    for cuota in (Decimal("1.000"), Decimal("1.788"), Decimal("7.420"), Decimal("20.000")):
        assert de_milesimas(a_milesimas(cuota)) == cuota


def test_a_centavos_valores_conocidos():
    assert a_centavos(Decimal("500")) == 50000
    assert a_centavos(Decimal("0.01")) == 1


def test_a_milesimas_valores_conocidos():
    assert a_milesimas(Decimal("1.788")) == 1788
    assert a_milesimas(Decimal("1.264")) == 1264
