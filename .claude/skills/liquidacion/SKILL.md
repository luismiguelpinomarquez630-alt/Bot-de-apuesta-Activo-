---
name: liquidacion
description: Reglas de liquidación de mercados en Betcubandreams. Usar siempre que se toque settlement, payouts, score_final_confirmado, o el módulo de fallback de resultados (fallback_resultados.py).
---

# Reglas de liquidación — no negociables

La resolución de mercados (qué gana, qué pierde, qué queda nulo, cómo se
liquidan combinadas, redondeo, tests obligatorios) vive en
`REGLAS_LIQUIDACION.md`, en la raíz del repo. Ese documento es la única fuente
de verdad para `settlement_engine.py` y su función `resolver()` — no se
duplica acá. Lo que sigue es proceso, no lógica de resolución:

1. Nunca liquidar un mercado con un dato que no esté marcado como `score_final_confirmado`.
2. Todo cambio en código de settlement pasa primero por dry-run antes de tocar producción.
3. Eventos resueltos vía la red de fallback (ESPN u otra fuente secundaria) requieren
   confirmación admin explícita antes de liquidar. Nunca auto-liquidar desde fallback.
4. CI debe estar en verde antes de mergear a `main` (Railway deploya automáticamente desde main).
5. Diagnóstico antes que fix: primero confirmar la causa raíz del bug, después tocar código.

# Contexto técnico
- Runtime: Python 3.12, python-telegram-bot v20
- DB: SQLite en modo WAL, path `/data/betcuba.db`
- Scheduler: APScheduler
- Deploy: Railway, rama `main`

# Convención de nombres V2 (spec — Claude Code debe implementar así)
- Módulo que ejecuta la liquidación final: `settlement_engine.py`
- Módulo de cascada de fuentes de resultados: `fuente_resultados/cascada_fuentes.py`
- Tabla de mercados: `markets` — columna booleana `score_final_confirmado`
- Tabla de liquidaciones: `payouts`
- Flag de bloqueo dry-run (creado manualmente antes de tocar settlement en prod):
  `.claude/DRY_RUN_OK`

Si durante la implementación Claude Code necesita desviarse de estos nombres,
debe actualizar también `.claude/hooks/check-dry-run.sh` (variable `PATTERN`)
en el mismo commit — nunca dejar el hook desincronizado del código real.

# Al trabajar en este dominio
- Si vas a editar código de liquidación, primero verificá si existe `.claude/DRY_RUN_OK`
  (el hook `check-dry-run.sh` lo exige). Si no existe, corré el dry-run correspondiente
  antes de pedir la edición.
- Si vas a tocar `resolver()` o cualquier regla de mercado, es contra
  `REGLAS_LIQUIDACION.md` — nunca inventar una regla nueva sin agregarla ahí
  primero, y nunca mergear sin los tests de su §8.
- Después de cualquier cambio en estos módulos, correr `pytest tests/ -q` y confirmar verde
  antes de considerar el trabajo terminado.
