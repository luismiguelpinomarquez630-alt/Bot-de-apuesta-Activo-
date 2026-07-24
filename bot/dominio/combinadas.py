"""Validación estructural de combinadas (LIMITES.md §7).

CAPA 0 (CLAUDE.md regla 8). No importa de ninguna otra capa. Recibe
primitivos, no tipos de `core/` — el display de Telegram también la
necesitará, sin arrastrar `core/`.
"""

COMBI_MAX_PATAS = 4
COMBI_CUOTA_MAX_MILESIMAS = 20_000  # LIMITES.md §2, cuota 20,0


def combinada_valida(game_ids: list[int], cuota_total_milesimas: int) -> bool:
    """2..COMBI_MAX_PATAS patas, una sola por `game_id` (COMBI_MISMO_PARTIDO
    prohibido, LIMITES §7), `cuota_total` dentro de `COMBI_CUOTA_MAX_MILESIMAS`.

    La moneda es responsabilidad del llamador: en `core/apuestas.py` todas
    las patas de una solicitud comparten una única moneda, así que no hay
    nada que comparar acá.
    """
    if not 2 <= len(game_ids) <= COMBI_MAX_PATAS:
        return False
    if len(set(game_ids)) != len(game_ids):
        return False
    if cuota_total_milesimas > COMBI_CUOTA_MAX_MILESIMAS:
        return False
    return True
