"""Liquidación de tickets (apuestas simples y combinadas), Fase 1.

Implementa REGLAS_LIQUIDACION.md §6 (precedencia y combinadas) y §7.1
(aritmética y redondeo). Función `resolver_ticket()` PURA: sin DB, sin red,
sin efectos secundarios. Depende de `EstadoSeleccion`
(`bot/core/resolucion_mercados.py`), el resultado de resolver cada pata — no
vuelve a decidir eso, solo agrega.

Redondeo (REGLAS_LIQUIDACION §7.1): la cuota efectiva de la combinada se
redondea a milésimas ANTES de calcular el payout, no se acumula en precisión
completa. Es la única forma de que el resultado coincida con
`tickets.cuota_milesimas`/`payout_pot_cent`, que se calculan igual al aceptar
(`ESQUEMA_DB.md` §1.6) y son enteros — no hay dónde guardar los hasta 12
decimales de multiplicar 4 cuotas de 3 decimales cada una.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

from bot.core.resolucion_mercados import EstadoSeleccion

MIL = Decimal("1000")


class EstadoTicket(Enum):
    PENDIENTE = "pendiente"
    GANADO = "ganado"
    PERDIDO = "perdido"
    NULO = "nulo"
    REQUIERE_ADMIN = "requiere_admin"


@dataclass(frozen=True)
class PataResuelta:
    estado: EstadoSeleccion
    cuota_milesimas: int


@dataclass(frozen=True)
class TicketResuelto:
    estado: EstadoTicket
    cuota_efectiva_milesimas: int
    payout_cent: int


def resolver_ticket(patas: list[PataResuelta], stake_cent: int) -> TicketResuelto:
    """Resuelve un ticket a partir del estado ya resuelto de cada pata.

    Orden de precedencia (REGLAS_LIQUIDACION §6), el primero que dispara gana:

        1. alguna REQUIERE_ADMIN → REQUIERE_ADMIN
        2. alguna PERDIDA        → PERDIDO
        3. alguna PENDIENTE      → PENDIENTE
        4. resto (GANADA/NULA)   → GANADO

    PERDIDA le gana a PENDIENTE: una pata perdida condena el ticket
    matemáticamente, ninguna pata pendiente puede revertirlo, y esperarla solo
    retiene exposición sobre algo que ya no puede pagar (`LIMITES.md` §4).
    REQUIERE_ADMIN sigue yendo primero, incluso antes que PERDIDA: una
    anomalía puede tener la misma causa raíz que la pata marcada perdida — si
    el dato de origen está mal, "perdida" también podría estarlo. Una espera
    normal (PENDIENTE) no tiene ese riesgo.

    En los tres casos que no son GANADO, `cuota_efectiva_milesimas` y
    `payout_cent` son 0: todavía no hay nada que pagar.

    Lista de patas vacía → ValueError (un ticket sin patas es un bug del
    llamador, no un caso de negocio).
    """
    if not patas:
        raise ValueError("un ticket necesita al menos una pata")

    estados = [p.estado for p in patas]

    if EstadoSeleccion.REQUIERE_ADMIN in estados:
        return TicketResuelto(EstadoTicket.REQUIERE_ADMIN, 0, 0)

    if EstadoSeleccion.PERDIDA in estados:
        return TicketResuelto(EstadoTicket.PERDIDO, 0, 0)

    if EstadoSeleccion.PENDIENTE in estados:
        return TicketResuelto(EstadoTicket.PENDIENTE, 0, 0)

    # Solo quedan GANADA y NULA. Una pata NULA aporta cuota 1.000: no suma,
    # pero tampoco anula el ticket (REGLAS_LIQUIDACION §6).
    producto = Decimal(1)
    for p in patas:
        if p.estado == EstadoSeleccion.GANADA:
            producto *= Decimal(p.cuota_milesimas) / MIL

    cuota_efectiva_milesimas = int((producto * MIL).to_integral_value(ROUND_HALF_UP))
    payout_cent = int(
        (Decimal(stake_cent) * Decimal(cuota_efectiva_milesimas) / MIL).to_integral_value(
            ROUND_HALF_UP
        )
    )
    return TicketResuelto(EstadoTicket.GANADO, cuota_efectiva_milesimas, payout_cent)
