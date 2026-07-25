from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from bot.dominio.tiempo import inicio_de_dia_habana

HABANA = ZoneInfo("America/Havana")


def test_trunca_a_medianoche_habana():
    ts = int(datetime(2024, 1, 15, 15, 30, tzinfo=HABANA).timestamp())
    esperado = int(datetime(2024, 1, 15, 0, 0, tzinfo=HABANA).timestamp())
    assert inicio_de_dia_habana(ts) == esperado


def test_mismo_dia_habana_da_el_mismo_inicio():
    ts_manana = int(datetime(2024, 1, 15, 6, 0, tzinfo=HABANA).timestamp())
    ts_noche = int(datetime(2024, 1, 15, 23, 0, tzinfo=HABANA).timestamp())
    assert inicio_de_dia_habana(ts_manana) == inicio_de_dia_habana(ts_noche)


def test_dias_distintos_dan_inicios_86400s_aparte():
    ts_dia1 = int(datetime(2024, 1, 15, 12, 0, tzinfo=HABANA).timestamp())
    ts_dia2 = int(datetime(2024, 1, 16, 12, 0, tzinfo=HABANA).timestamp())

    assert inicio_de_dia_habana(ts_dia2) - inicio_de_dia_habana(ts_dia1) == 86400


def test_usa_dst_no_offset_fijo():
    # Enero: horario estandar de Cuba (UTC-5). Julio: horario de verano (UTC-4).
    ts_enero = int(datetime(2024, 1, 15, 12, 0, tzinfo=HABANA).timestamp())
    ts_julio = int(datetime(2024, 7, 15, 12, 0, tzinfo=HABANA).timestamp())

    medianoche_utc_enero = datetime(2024, 1, 15, tzinfo=timezone.utc).timestamp()
    medianoche_utc_julio = datetime(2024, 7, 15, tzinfo=timezone.utc).timestamp()

    assert inicio_de_dia_habana(ts_enero) - medianoche_utc_enero == 5 * 3600
    assert inicio_de_dia_habana(ts_julio) - medianoche_utc_julio == 4 * 3600
