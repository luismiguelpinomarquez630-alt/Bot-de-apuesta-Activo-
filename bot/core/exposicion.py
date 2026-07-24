"""Exposición por evento y global — límites de riesgo (LIMITES.md §4-§5).

Solo lectura sobre la base: ninguna función acá escribe, acepta apuestas ni
conoce marcadores. Aceptar la apuesta es responsabilidad de `core/apuestas.py`.

Decisión de diseño: SOBRE-CÁLCULO (LIMITES.md §4, "Implementación elegida para
Fase 1"). `exposicion_evento` suma el `payout_pot_cent` de TODOS los tickets
pendientes que tocan un `game_id`, sin enumerar resultados posibles ni
distinguir selecciones excluyentes entre sí. Es un límite superior del peor
caso real (asume que todas las apuestas del evento ganan a la vez), elegido a
propósito porque:

- nunca subestima el riesgo — es el lado seguro de un límite
- es O(n), una suma, sin enumerar resultados
- no depende de `resolucion_mercados.resolver()` ni de conocer el marcador

El cálculo exacto por escenarios queda para más adelante si hiciera falta
afinar el límite.
"""

import sqlite3
from dataclasses import dataclass

# LIMITES.md §1 — banca por moneda, en centavos. Cada moneda es un libro
# contable independiente: los porcentajes de abajo nunca cruzan monedas.
BANCAS = {"CUP": 50000_00, "USD": 100_00}

# LIMITES.md §2 — porcentajes de la banca. Todo en aritmética entera: con
# estos valores de BANCAS la división es exacta, nunca se trunca de más.
EXPOSICION_MAX_EVENTO = {moneda: banca * 10 // 100 for moneda, banca in BANCAS.items()}
EXPOSICION_MAX_GLOBAL = {moneda: banca * 30 // 100 for moneda, banca in BANCAS.items()}


@dataclass(frozen=True)
class ResultadoExposicion:
    cabe: bool
    motivo: str
    limite_excedido: str | None  # "EXPOSICION_MAX_EVENTO" | "EXPOSICION_MAX_GLOBAL" | None


def exposicion_evento(conn: sqlite3.Connection, game_id: int, moneda: str) -> int:
    """Suma de `payout_pot_cent` de tickets pendientes con una selección
    pendiente en `game_id`, en `moneda`. Centavos.

    Un ticket con varias patas en el mismo `game_id` (no debería darse por la
    regla de combinadas de LIMITES §7, pero por si acaso) se cuenta UNA sola
    vez: el `DISTINCT` es sobre `(t.id, t.payout_pot_cent)`, que colapsa las
    filas duplicadas que produce el JOIN antes de sumar.
    """
    fila = conn.execute(
        "SELECT COALESCE(SUM(payout_pot_cent), 0) FROM ("
        "  SELECT DISTINCT t.id, t.payout_pot_cent "
        "  FROM tickets t JOIN selecciones s ON s.ticket_id = t.id "
        "  WHERE s.game_id = ? AND s.estado = 'pendiente' "
        "    AND t.estado = 'pendiente' AND t.moneda = ?"
        ")",
        (game_id, moneda),
    ).fetchone()
    return fila[0]


def exposicion_global(conn: sqlite3.Connection, moneda: str) -> int:
    """Suma de `payout_pot_cent` de todos los tickets pendientes de `moneda`.
    Cada ticket una vez: no hace falta JOIN, cada fila de `tickets` ya es un
    ticket distinto."""
    fila = conn.execute(
        "SELECT COALESCE(SUM(payout_pot_cent), 0) FROM tickets "
        "WHERE estado = 'pendiente' AND moneda = ?",
        (moneda,),
    ).fetchone()
    return fila[0]


def cabe_apuesta(
    conn: sqlite3.Connection,
    moneda: str,
    game_ids: list[int],
    payout_nuevo_cent: int,
    ahora_ts: int,
) -> ResultadoExposicion:
    """Simula añadir un ticket nuevo que toca `game_ids` con payout potencial
    `payout_nuevo_cent`. `game_ids` es la lista de partidos del ticket nuevo
    (uno si es simple, N si es combinada): el payout completo del ticket
    nuevo cuenta contra CADA evento que toca (LIMITES §4).

    Corta en el primer límite que no entra: cada evento de `game_ids` contra
    `EXPOSICION_MAX_EVENTO`, y si todos entran, el total contra
    `EXPOSICION_MAX_GLOBAL`.
    """
    limite_evento = EXPOSICION_MAX_EVENTO[moneda]
    for game_id in game_ids:
        proyectada = exposicion_evento(conn, game_id, moneda) + payout_nuevo_cent
        if proyectada > limite_evento:
            return ResultadoExposicion(
                cabe=False,
                motivo=(
                    f"el evento {game_id} superaría EXPOSICION_MAX_EVENTO "
                    f"({proyectada} > {limite_evento} centavos {moneda})"
                ),
                limite_excedido="EXPOSICION_MAX_EVENTO",
            )

    limite_global = EXPOSICION_MAX_GLOBAL[moneda]
    proyectada_global = exposicion_global(conn, moneda) + payout_nuevo_cent
    if proyectada_global > limite_global:
        return ResultadoExposicion(
            cabe=False,
            motivo=(
                f"la exposición global superaría EXPOSICION_MAX_GLOBAL "
                f"({proyectada_global} > {limite_global} centavos {moneda})"
            ),
            limite_excedido="EXPOSICION_MAX_GLOBAL",
        )

    return ResultadoExposicion(cabe=True, motivo="dentro de los límites de exposición", limite_excedido=None)
