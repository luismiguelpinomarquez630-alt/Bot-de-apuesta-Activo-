"""Convención de tiempo del proyecto (CLAUDE.md): toda fecha de cara al
usuario es hora de Cuba, todo timestamp interno es epoch UTC. Este módulo es
el único borde de conversión — ningún otro hace su propia aritmética de
zona horaria.

CAPA 0. No importa de ninguna otra capa.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

_HABANA = ZoneInfo("America/Havana")  # nunca un offset fijo: Cuba tiene DST


def inicio_de_dia_habana(ts: int) -> int:
    """Epoch UTC del inicio (00:00) del día de Cuba al que pertenece `ts`."""
    local = datetime.fromtimestamp(ts, tz=_HABANA)
    inicio_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(inicio_local.timestamp())
