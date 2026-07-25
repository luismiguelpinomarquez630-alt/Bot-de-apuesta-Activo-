from bot.dominio.combinadas import COMBI_CUOTA_MAX_MILESIMAS, COMBI_MAX_PATAS, combinada_valida


def test_combinada_valida_caso_tipico():
    assert combinada_valida([1, 2, 3], 5000) is True


def test_rechaza_una_sola_pata():
    assert combinada_valida([1], 1500) is False


def test_rechaza_mas_de_combi_max_patas():
    assert combinada_valida(list(range(COMBI_MAX_PATAS + 1)), 1500) is False


def test_acepta_en_combi_max_patas_exacto():
    assert combinada_valida(list(range(COMBI_MAX_PATAS)), 1500) is True


def test_rechaza_game_id_repetido():
    assert combinada_valida([1, 1, 2], 1500) is False


def test_rechaza_cuota_total_por_encima_del_maximo():
    assert combinada_valida([1, 2], COMBI_CUOTA_MAX_MILESIMAS + 1) is False


def test_acepta_cuota_total_en_el_limite_exacto():
    assert combinada_valida([1, 2], COMBI_CUOTA_MAX_MILESIMAS) is True
