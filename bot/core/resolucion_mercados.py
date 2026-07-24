"""Resolución de mercados de fútbol, Fase 1.

Implementado contra REGLAS_LIQUIDACION.md §5, tal cual — sin interpretación
propia. Función `resolver()` PURA: sin DB, sin red, sin efectos secundarios.
No decide validez ni filtra mercados no habilitados: eso es la capa de
presentación (REGLAS_LIQUIDACION.md §9). Un tipo no soportado o un parámetro
inválido acá es un bug del llamador, no un caso de negocio — levanta
`ValueError`, nunca `REQUIERE_ADMIN`.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class EstadoSeleccion(Enum):
    GANADA = "ganada"
    PERDIDA = "perdida"
    NULA = "nula"
    PENDIENTE = "pendiente"
    REQUIERE_ADMIN = "requiere_admin"


@dataclass(frozen=True)
class Marcador:
    local: int
    visitante: int
    periodos_raw: str


_TIPOS_CON_PARAMETRO = frozenset({7, 8, 9, 10, 11, 12, 13, 14})
_TIPOS_SIN_PARAMETRO = frozenset({1, 2, 3, 4, 5, 6, 180, 181})
_TIPOS_SOPORTADOS = _TIPOS_CON_PARAMETRO | _TIPOS_SIN_PARAMETRO


def parametro_valido(p: Decimal | None) -> bool:
    """Solo líneas enteras o de medio punto (REGLAS_LIQUIDACION §1). Los
    cuartos (asiáticas) parten el stake en dos y se rechazan en Fase 1."""
    if p is None:
        return True
    return (p * 2) % 1 == 0


def tipo_soportado(t: int) -> bool:
    """Exactamente los tipos verificados de REGLAS_LIQUIDACION §5."""
    return t in _TIPOS_SOPORTADOS


def resolver(tipo: int, parametro: Decimal | None, m: Marcador) -> EstadoSeleccion:
    """Resuelve una selección según REGLAS_LIQUIDACION §5.1-§5.6.

    Un `tipo` no soportado o un `parametro` inválido levanta `ValueError`:
    ese filtro va antes, en presentación (§9). No se enmascara acá como
    `REQUIERE_ADMIN`, porque eso ocultaría un bug de la capa de arriba
    disfrazado de caso de negocio.
    """
    if not tipo_soportado(tipo):
        raise ValueError(f"tipo {tipo} no soportado (REGLAS_LIQUIDACION §5)")
    if tipo in _TIPOS_CON_PARAMETRO and parametro is None:
        raise ValueError(f"tipo {tipo} requiere parametro (REGLAS_LIQUIDACION §5)")
    if tipo in _TIPOS_SIN_PARAMETRO and parametro is not None:
        raise ValueError(f"tipo {tipo} no admite parametro (REGLAS_LIQUIDACION §5)")
    if not parametro_valido(parametro):
        raise ValueError(
            f"parametro {parametro} inválido: solo múltiplos de 0.5 (REGLAS_LIQUIDACION §1)"
        )

    G = EstadoSeleccion.GANADA
    P = EstadoSeleccion.PERDIDA
    N = EstadoSeleccion.NULA

    # --- 5.1 Resultado 1X2 — sin parámetro, nunca produce NULA ---
    if tipo == 1:
        return G if m.local > m.visitante else P
    if tipo == 2:
        return G if m.local == m.visitante else P
    if tipo == 3:
        return G if m.visitante > m.local else P

    # --- 5.2 Doble oportunidad — sin parámetro, nunca produce NULA ---
    if tipo == 4:
        return G if m.local >= m.visitante else P
    if tipo == 5:
        return G if m.local != m.visitante else P
    if tipo == 6:
        return G if m.visitante >= m.local else P

    # --- 5.3 Hándicap — requiere parámetro ---
    # T7 suma el parámetro al LOCAL, T8 al VISITANTE. Invertirlo es el error
    # silencioso más caro de esta categoría.
    if tipo == 7:
        dif = (m.local + parametro) - m.visitante
        if dif > 0:
            return G
        if dif == 0:
            return N
        return P
    if tipo == 8:
        dif = (m.visitante + parametro) - m.local
        if dif > 0:
            return G
        if dif == 0:
            return N
        return P

    # --- 5.4 Total de goles — requiere parámetro ---
    if tipo == 9:
        total = m.local + m.visitante
        if total > parametro:
            return G
        if total == parametro:
            return N
        return P
    if tipo == 10:
        total = m.local + m.visitante
        if total < parametro:
            return G
        if total == parametro:
            return N
        return P

    # --- 5.5 Total individual — requiere parámetro ---
    # T11/T12 son del LOCAL, T13/T14 del VISITANTE. Solo se nota invertido
    # con un marcador asimétrico (§8, test obligatorio).
    if tipo in (11, 12, 13, 14):
        equipo = m.local if tipo in (11, 12) else m.visitante
        mas = tipo in (11, 13)
        if equipo == parametro:
            return N
        if mas:
            return G if equipo > parametro else P
        return G if equipo < parametro else P

    # --- 5.6 Ambos equipos marcan — sin parámetro, nunca produce NULA ---
    ambos = m.local >= 1 and m.visitante >= 1
    if tipo == 180:
        return G if ambos else P
    return P if ambos else G  # tipo == 181
