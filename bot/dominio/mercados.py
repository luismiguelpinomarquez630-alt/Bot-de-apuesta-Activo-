"""Catálogo de dominio: tipos de mercado soportados y validación de líneas.

CAPA 0 (CLAUDE.md regla 8). No importa de ninguna otra capa del proyecto.
Cualquier capa puede importar de acá; nunca al revés.
"""

from decimal import Decimal

TIPOS_SOPORTADOS = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 180, 181})


def linea_valida(p: Decimal | None) -> bool:
    """Solo líneas enteras o de medio punto (REGLAS_LIQUIDACION §1). Los
    cuartos (asiáticas) parten el stake en dos y se rechazan en Fase 1."""
    if p is None:
        return True
    return (p * 2) % 1 == 0
