import pytest

from bot.core.liquidacion_tickets import EstadoTicket, PataResuelta, resolver_ticket
from bot.core.resolucion_mercados import EstadoSeleccion

GANADA = EstadoSeleccion.GANADA
PERDIDA = EstadoSeleccion.PERDIDA
NULA = EstadoSeleccion.NULA
PENDIENTE = EstadoSeleccion.PENDIENTE
REQUIERE_ADMIN = EstadoSeleccion.REQUIERE_ADMIN


def _pata(estado, cuota_milesimas=1500):
    return PataResuelta(estado=estado, cuota_milesimas=cuota_milesimas)


# --- Los 6 casos de §8 -----------------------------------------------------


def test_todas_ganadas_producto_de_cuotas():
    patas = [_pata(GANADA, 2000), _pata(GANADA, 1500)]  # 2.000 * 1.500 = 3.000
    resultado = resolver_ticket(patas, stake_cent=1000)
    assert resultado.estado == EstadoTicket.GANADO
    assert resultado.cuota_efectiva_milesimas == 3000
    assert resultado.payout_cent == 3000  # 10.00 * 3.000 = 30.00


def test_una_perdida_perdido():
    patas = [_pata(GANADA), _pata(PERDIDA)]
    resultado = resolver_ticket(patas, stake_cent=1000)
    assert resultado.estado == EstadoTicket.PERDIDO
    assert resultado.cuota_efectiva_milesimas == 0
    assert resultado.payout_cent == 0


def test_una_nula_resto_ganadas_producto_sin_la_nula():
    patas = [_pata(GANADA, 2000), _pata(NULA, 9999), _pata(GANADA, 1500)]
    resultado = resolver_ticket(patas, stake_cent=1000)
    assert resultado.estado == EstadoTicket.GANADO
    assert resultado.cuota_efectiva_milesimas == 3000  # 2.000 * 1.500, la nula no cuenta
    assert resultado.payout_cent == 3000


def test_todas_nulas_cuota_1000_devuelve_el_stake():
    patas = [_pata(NULA, 1234), _pata(NULA, 5678)]
    resultado = resolver_ticket(patas, stake_cent=5000)
    assert resultado.estado == EstadoTicket.NULO
    assert resultado.cuota_efectiva_milesimas == 1000
    assert resultado.payout_cent == 5000  # el stake íntegro, exacto


def test_una_pendiente_pendiente():
    patas = [_pata(GANADA), _pata(PENDIENTE)]
    resultado = resolver_ticket(patas, stake_cent=1000)
    assert resultado.estado == EstadoTicket.PENDIENTE
    assert resultado.cuota_efectiva_milesimas == 0
    assert resultado.payout_cent == 0


def test_una_requiere_admin_requiere_admin():
    patas = [_pata(GANADA), _pata(REQUIERE_ADMIN)]
    resultado = resolver_ticket(patas, stake_cent=1000)
    assert resultado.estado == EstadoTicket.REQUIERE_ADMIN
    assert resultado.cuota_efectiva_milesimas == 0
    assert resultado.payout_cent == 0


# --- Casos adicionales pedidos ----------------------------------------------


def test_todas_nulas_payout_exacto_igual_al_stake():
    patas = [_pata(NULA), _pata(NULA), _pata(NULA)]
    resultado = resolver_ticket(patas, stake_cent=123456)
    assert resultado.estado == EstadoTicket.NULO
    assert resultado.payout_cent == 123456


def test_ticket_una_sola_pata_nula_es_nulo():
    patas = [_pata(NULA, 1788)]
    resultado = resolver_ticket(patas, stake_cent=50000)
    assert resultado.estado == EstadoTicket.NULO
    assert resultado.cuota_efectiva_milesimas == 1000
    assert resultado.payout_cent == 50000


def test_una_nula_tres_ganadas_producto_sin_la_nula():
    patas = [_pata(GANADA, 2000), _pata(GANADA, 1500), _pata(NULA, 4321), _pata(GANADA, 1200)]
    resultado = resolver_ticket(patas, stake_cent=1000)
    # 2.000 * 1.500 * 1.200 = 3.600
    assert resultado.cuota_efectiva_milesimas == 3600
    assert resultado.estado == EstadoTicket.GANADO


def test_perdida_y_requiere_admin_requiere_admin_no_perdido():
    patas = [_pata(PERDIDA), _pata(REQUIERE_ADMIN)]
    resultado = resolver_ticket(patas, stake_cent=1000)
    assert resultado.estado == EstadoTicket.REQUIERE_ADMIN


def test_perdida_y_pendiente_perdido():
    patas = [_pata(PERDIDA), _pata(PENDIENTE)]
    resultado = resolver_ticket(patas, stake_cent=1000)
    assert resultado.estado == EstadoTicket.PERDIDO


def test_lista_vacia_levanta_value_error():
    with pytest.raises(ValueError):
        resolver_ticket([], stake_cent=1000)


def test_ticket_una_sola_pata_ganada_payout_stake_por_cuota():
    patas = [_pata(GANADA, 1788)]
    resultado = resolver_ticket(patas, stake_cent=50000)
    assert resultado.cuota_efectiva_milesimas == 1788
    # 500.00 * 1.788 = 894.00
    assert resultado.payout_cent == 89400


def test_redondeo_half_up_en_punto_5_exacto():
    # Un pata con cuota 2.005, stake 1.00 -> payout exacto 2.005 -> centavos
    # 200.5 exacto: ROUND_HALF_UP redondea a 201, ROUND_HALF_EVEN daria 200.
    patas = [_pata(GANADA, 2005)]
    resultado = resolver_ticket(patas, stake_cent=100)
    assert resultado.payout_cent == 201
