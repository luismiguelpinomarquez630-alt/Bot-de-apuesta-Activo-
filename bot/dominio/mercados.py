"""Catálogo de dominio: tipos de mercado soportados y validación de líneas.

CAPA 0 (CLAUDE.md regla 8). No importa de ninguna otra capa del proyecto.
Cualquier capa puede importar de acá; nunca al revés.
"""

from decimal import ROUND_HALF_UP, Decimal

TIPOS_SOPORTADOS = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 180, 181})


def linea_valida(p: Decimal | None) -> bool:
    """Solo líneas enteras o de medio punto (REGLAS_LIQUIDACION §1). Los
    cuartos (asiáticas) parten el stake en dos y se rechazan en Fase 1."""
    if p is None:
        return True
    return (p * 2) % 1 == 0


def linea_a_centesimas(p: Decimal) -> int:
    """Convierte la línea de un mercado (hándicap o total, ej. 2.5) a su
    representación entera en `selecciones.parametro_centesimas` (2.5 -> 250).

    ⚠️ NO es una conversión de dinero — no usar `bot.db.conversion.a_centavos`
    para esto. Comparten el factor 100 por coincidencia, no por equivalencia:
    una línea deportiva no es un monto, y acoplar su representación a la
    monetaria es un contrato implícito que se rompe en silencio si alguna de
    las dos cambia de escala."""
    return int((p * 100).to_integral_value(ROUND_HALF_UP))


def centesimas_a_linea(c: int) -> Decimal:
    """Inversa de `linea_a_centesimas`: reconstruye la línea de un mercado a
    partir de `selecciones.parametro_centesimas` (250 -> 2.5)."""
    return Decimal(c) / 100
