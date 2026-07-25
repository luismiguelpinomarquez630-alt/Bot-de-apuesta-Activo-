"""Motor de liquidación.

CLAUDE.md regla 4: el ÚNICO módulo que escribe movimientos `payout` y
`devolucion`. Orquesta `cascada_fuentes.evaluar()`, `resolucion_mercados.
resolver()` y `liquidacion_tickets.resolver_ticket()` — no reimplementa
ninguna de las tres.

Modelo de liquidación: automático. `REQUIERE_ADMIN` es la única excepción —
esos tickets se marcan y quedan para revisión humana, nunca se pagan solos.
"""

import sqlite3
import time
from dataclasses import dataclass

from bot.core.liquidacion_tickets import EstadoTicket, PataResuelta, resolver_ticket
from bot.core.resolucion_mercados import EstadoSeleccion, Marcador, resolver
from bot.dominio.mercados import centesimas_a_linea
from bot.fuente_resultados import cascada_fuentes
from bot.fuente_resultados.primaria import cliente_1x

# Momento en que arrancó ESTE proceso. Capturado una sola vez al importar el
# módulo — no es un parámetro de función porque no varía dentro de una misma
# corrida, es una propiedad del proceso (análogo a su PID). La usa la guarda
# de arranque de _evaluar_seleccion().
ARRANQUE_TS: int = int(time.time())


@dataclass(frozen=True)
class ResultadoLiquidacion:
    ticket_id: int
    estado: EstadoTicket
    movimiento_insertado: bool


@dataclass(frozen=True)
class _SeleccionEvaluada:
    seleccion_id: int
    estado: EstadoSeleccion
    marcador_raw: str | None
    cuota_milesimas: int


async def _evaluar_seleccion(
    conn: sqlite3.Connection,
    seleccion_id: int,
    game_id: int,
    champ_id: int,
    date_start: int,
    ahora_ts: int,
    market_type: int,
    parametro_centesimas: int | None,
    cuota_milesimas: int,
) -> _SeleccionEvaluada:
    """Evalúa una selección y la traduce al estado de su pata.

    TX1: lee el estado previo de la observación (para la guarda de arranque),
    llama evaluar() —que escribe en observaciones_resultado— y comitea de
    inmediato, sin importar el resultado. Nunca se abre una transacción que
    abarque esto y el pago (ESQUEMA_DB §1.6 / REGLAS_LIQUIDACION §7.1).
    """
    fila_previa = conn.execute(
        "SELECT ultima_consulta_ts FROM observaciones_resultado WHERE game_id = ?",
        (game_id,),
    ).fetchone()

    resultado_eval = await cascada_fuentes.evaluar(game_id, champ_id, date_start, ahora_ts, conn)
    conn.commit()  # TX1 — siempre, gane o no gane el ticket.

    if resultado_eval.estado == cascada_fuentes.EstadoResultado.REQUIERE_ADMIN:
        return _SeleccionEvaluada(
            seleccion_id, EstadoSeleccion.REQUIERE_ADMIN, resultado_eval.marcador_raw, cuota_milesimas
        )

    if resultado_eval.estado == cascada_fuentes.EstadoResultado.NO_CONFIRMADO:
        return _SeleccionEvaluada(seleccion_id, EstadoSeleccion.PENDIENTE, None, cuota_milesimas)

    # CONFIRMADO. Guarda de arranque: si YA existía una fila antes de esta
    # llamada y su última consulta es anterior al arranque de este proceso,
    # la estabilidad que confirma pudo haberse fijado con datos de antes de
    # un apagón, que 1x pudo corregir mientras el bot estaba caído. Se exige
    # al menos una consulta fresca previa (no la de ahora mismo, que
    # evaluar() acaba de escribir) antes de confiar en CONFIRMADO.
    #
    # Si NUNCA hubo fila previa, este resguardo no aplica: la propia guarda
    # de estabilidad de 15 min de cascada_fuentes ya impide CONFIRMADO en la
    # primera observación de un partido, así que no hay dato pre-apagón que
    # pudiera estar revalidándose sin querer.
    if fila_previa is not None and fila_previa[0] <= ARRANQUE_TS:
        return _SeleccionEvaluada(seleccion_id, EstadoSeleccion.PENDIENTE, None, cuota_milesimas)

    # Defensa en profundidad: cascada_fuentes garantiza que marcador_raw es
    # parseable si dijo CONFIRMADO, pero esta capa paga dinero real y no
    # confía en garantías ajenas sin verificarlas ella misma.
    marcador_parseado = cliente_1x.parse_score(resultado_eval.marcador_raw)
    if marcador_parseado is None:
        return _SeleccionEvaluada(
            seleccion_id, EstadoSeleccion.REQUIERE_ADMIN, resultado_eval.marcador_raw, cuota_milesimas
        )

    m = Marcador(
        local=marcador_parseado["local"],
        visitante=marcador_parseado["visitante"],
        periodos_raw=marcador_parseado["periodos_raw"],
    )
    parametro = centesimas_a_linea(parametro_centesimas) if parametro_centesimas is not None else None
    estado_pata = resolver(market_type, parametro, m)
    return _SeleccionEvaluada(seleccion_id, estado_pata, resultado_eval.marcador_raw, cuota_milesimas)


def _insertar_movimiento(
    conn: sqlite3.Connection,
    usuario_id: int,
    moneda: str,
    ticket_id: int,
    tipo: str,
    centavos: int,
    ahora_ts: int,
) -> bool:
    """Inserta el movimiento y actualiza saldos. Devuelve False sin lanzar si
    ux_mov_pago_unico ya existía: no es un error, es la idempotencia
    funcionando (ESQUEMA_DB §1.5)."""
    try:
        conn.execute(
            "INSERT INTO movimientos (usuario_id, moneda, centavos, tipo, ticket_id, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (usuario_id, moneda, centavos, tipo, ticket_id, ahora_ts),
        )
    except sqlite3.IntegrityError:
        return False
    conn.execute(
        "UPDATE saldos SET centavos = centavos + ? WHERE usuario_id = ? AND moneda = ?",
        (centavos, usuario_id, moneda),
    )
    return True


async def liquidar_ticket(ticket_id: int, ahora_ts: int, conn: sqlite3.Connection) -> ResultadoLiquidacion:
    """Liquida un ticket. Idempotente: correrla dos veces sobre el mismo
    ticket ya resuelto no genera un segundo movimiento.

    Flujo (por selección, con el estado que ya tiene guardado el ticket):
      1. evaluar() cada selección — game_id/champ_id/date_start salen de la
         propia fila de `selecciones` (`date_start` es `inicio_ts`: cascada_
         fuentes lo necesita y no puede leerlo de la respuesta si el partido
         no aparece).
      2. Traducir el resultado a la pata (ver _evaluar_seleccion).
      3. resolver_ticket() sobre el conjunto de patas.
      4. Si el ticket resuelve a estado terminal, persistir en una segunda
         transacción separada de la de las observaciones (TX2).
    """
    fila_ticket = conn.execute(
        "SELECT usuario_id, moneda, stake_cent, estado FROM tickets WHERE id = ?",
        (ticket_id,),
    ).fetchone()
    if fila_ticket is None:
        raise ValueError(f"ticket {ticket_id} no existe")

    usuario_id, moneda, stake_cent, estado_actual = fila_ticket
    if estado_actual != "pendiente":
        # Ya resuelto en una corrida anterior. No se reevalúa nada.
        return ResultadoLiquidacion(ticket_id, EstadoTicket(estado_actual), False)

    filas_selecciones = conn.execute(
        "SELECT id, game_id, champ_id, market_type, parametro_centesimas, "
        "cuota_milesimas, inicio_ts FROM selecciones WHERE ticket_id = ?",
        (ticket_id,),
    ).fetchall()

    evaluaciones = [
        await _evaluar_seleccion(
            conn,
            sel_id,
            game_id,
            champ_id,
            inicio_ts,  # date_start: viene de la selección guardada, no de otro lado
            ahora_ts,
            market_type,
            parametro_centesimas,
            cuota_milesimas,
        )
        for (sel_id, game_id, champ_id, market_type, parametro_centesimas, cuota_milesimas, inicio_ts) in filas_selecciones
    ]

    patas = [PataResuelta(estado=ev.estado, cuota_milesimas=ev.cuota_milesimas) for ev in evaluaciones]
    resultado_ticket = resolver_ticket(patas, stake_cent)

    if resultado_ticket.estado == EstadoTicket.PENDIENTE:
        # Ninguna escritura de ticket/selecciones/movimientos esta vuelta.
        # Las observaciones de TX1 ya quedaron comiteadas igual.
        return ResultadoLiquidacion(ticket_id, EstadoTicket.PENDIENTE, False)

    # TX2 — pago (o marca de estado), solo si hay un estado terminal.
    conn.execute("BEGIN IMMEDIATE")

    fila_actual = conn.execute("SELECT estado FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if fila_actual is None or fila_actual[0] != "pendiente":
        # Otro proceso ya lo tocó entre la lectura de arriba y este punto.
        conn.rollback()
        estado_final = EstadoTicket(fila_actual[0]) if fila_actual else resultado_ticket.estado
        return ResultadoLiquidacion(ticket_id, estado_final, False)

    for ev in evaluaciones:
        if ev.estado == EstadoSeleccion.PENDIENTE:
            continue  # esa selección en particular sigue sin resolver
        conn.execute(
            "UPDATE selecciones SET estado = ?, marcador_raw = ?, resuelto_ts = ? WHERE id = ?",
            (ev.estado.value, ev.marcador_raw, ahora_ts, ev.seleccion_id),
        )

    conn.execute(
        "UPDATE tickets SET estado = ?, resuelto_ts = ? WHERE id = ?",
        (resultado_ticket.estado.value, ahora_ts, ticket_id),
    )

    movimiento_insertado = False
    if resultado_ticket.estado == EstadoTicket.GANADO:
        movimiento_insertado = _insertar_movimiento(
            conn, usuario_id, moneda, ticket_id, "payout", resultado_ticket.payout_cent, ahora_ts
        )
    elif resultado_ticket.estado == EstadoTicket.NULO:
        movimiento_insertado = _insertar_movimiento(
            conn, usuario_id, moneda, ticket_id, "devolucion", stake_cent, ahora_ts
        )
    # PERDIDO: sin movimiento, el stake ya se debitó al aceptar. Insertar acá
    # lo cobraría dos veces. REQUIERE_ADMIN: sin movimiento, solo la marca.

    if not movimiento_insertado and resultado_ticket.estado in (EstadoTicket.GANADO, EstadoTicket.NULO):
        # ux_mov_pago_unico chocó: ya estaba pagado. Descartar todo lo
        # staged en esta TX (incluido el UPDATE de tickets/selecciones) es
        # la idempotencia funcionando, no un fallo.
        conn.rollback()
        return ResultadoLiquidacion(ticket_id, resultado_ticket.estado, False)

    conn.commit()
    return ResultadoLiquidacion(ticket_id, resultado_ticket.estado, movimiento_insertado)


async def liquidar_pendientes(ahora_ts: int, conn: sqlite3.Connection) -> list[ResultadoLiquidacion]:
    """Punto de entrada real: liquida todos los tickets 'pendiente'."""
    ids = [fila[0] for fila in conn.execute("SELECT id FROM tickets WHERE estado = 'pendiente'")]
    return [await liquidar_ticket(ticket_id, ahora_ts, conn) for ticket_id in ids]
