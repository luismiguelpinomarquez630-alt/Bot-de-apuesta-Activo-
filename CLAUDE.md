# Betcubandreams — contexto persistente del proyecto

Bot de apuestas en Telegram. Este es un repo reconstruido desde cero (V2) para
imponer separación real de capas desde el diseño, en vez de parchearla después
(lección de V1).

## Contexto técnico

- Runtime: Python 3.12, python-telegram-bot v20
- DB: SQLite en modo WAL, path `/data/betcuba.db`
- Scheduler: APScheduler
- Deploy: Railway, rama `main` (deploy automático desde `main`)

## Estructura de capas

```
bot/
  telegram/          # CAPA 3 — interacción con el usuario (handlers, keyboards)
  core/               # CAPA 2 — motor de negocio (settlement_engine.py, apuestas.py)
  fuente_resultados/  # CAPA 1 — datos externos (cascada_fuentes.py, confianza.py)
  db/                 # modelos y migraciones
  observabilidad/     # alertas y healthcheck
```

## Reglas de capas (no negociable)

1. `telegram/` nunca importa `fuente_resultados/` directamente. Solo habla con `core/`.
2. `core/settlement_engine.py` nunca llama a una fuente externa directo — siempre
   pasa por `fuente_resultados/cascada_fuentes.py`.
3. `cascada_fuentes.py` es la ÚNICA que decide si un resultado está en estado
   `confirmado`, `no_confirmado` o `requiere_admin`. Ningún otro módulo
   determina eso por su cuenta.
4. Ningún módulo escribe en la tabla `payouts` salvo `settlement_engine.py`,
   y solo cuando el resultado viene marcado `confirmado`.
5. Todo módulo bajo bot/fuente_resultados/ se implementa
   contra ESPECIFICACION_FUENTE.md. Los puntos marcados ❌
   en ese documento son supuestos NO verificados: está
   prohibido escribir código que dependa de ellos. Si al
   implementar aparece un supuesto que no está en el
   documento, se detiene, se verifica y se añade allí
   antes de escribir el código.

## Reglas de liquidación

Ver `.claude/skills/liquidacion/SKILL.md` para las reglas completas de settlement,
dry-run y fallback de resultados. Se aplican siempre que se toque `core/settlement_engine.py`,
`fuente_resultados/`, o cualquier código relacionado a `payouts` / `score_final_confirmado`.

## Estado actual

Este PR es solo el esqueleto de carpetas y gobernanza. No hay lógica de negocio
implementada todavía. La fuente primaria de resultados está pendiente de decisión
(trial de BetsAPI para las 4 ligas nicho) — `bot/fuente_resultados/primaria/` y
`bot/fuente_resultados/fallback/` están vacías a propósito.

## CI

`pytest` debe pasar en cada PR antes de mergear a `main` (ver `.github/workflows/ci.yml`).
Railway deploya automáticamente desde `main`, así que CI en verde es condición dura para mergear.
