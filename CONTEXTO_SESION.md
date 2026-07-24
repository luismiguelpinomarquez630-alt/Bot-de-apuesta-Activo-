# CONTEXTO_SESION.md — Betcubandreams V2

Repo: `luismiguelpinomarquez630-alt/Bot-de-apuesta-Activo-`

---

## Sesión 1 — Esqueleto y gobernanza

Merge commit `ce8f25c`, vía PR #1.

Reconstrucción desde cero. Se creó el esqueleto completo del proyecto y la capa
de gobernanza que fuerza la metodología de trabajo, antes de escribir cualquier
lógica de negocio.

### Estructura de carpetas

```
bot/
  telegram/          # CAPA 3 — interacción con el usuario
  core/              # CAPA 2 — motor de negocio
  fuente_resultados/ # CAPA 1 — datos externos
    primaria/
    fallback/
  db/
  observabilidad/
tests/
  unit/
  integration/
  conftest.py
.github/workflows/ci.yml
.claude/
  settings.json
  hooks/check-dry-run.sh
  skills/liquidacion/SKILL.md
CLAUDE.md
Procfile
railway.json
requirements.txt
```

### Reglas de capas (CLAUDE.md)

1. `telegram/` nunca importa `fuente_resultados/` directamente. Solo habla con `core/`.
2. `core/settlement_engine.py` nunca llama a una fuente externa directo — siempre
   pasa por `fuente_resultados/cascada_fuentes.py`.
3. `cascada_fuentes.py` es la ÚNICA que decide si un resultado está en estado
   `confirmado`, `no_confirmado` o `requiere_admin`.
4. Ningún módulo escribe en la tabla de payouts salvo `settlement_engine.py`, y
   solo cuando el resultado viene marcado `confirmado`.

### Gobernanza — hook de dry-run

`.claude/hooks/check-dry-run.sh` (PreToolUse). Convierte la metodología en código
determinista en vez de depender de que el modelo la recuerde.

- `Write`/`Edit` sobre archivos que matcheen el PATTERN → bloquea si no existe
  `.claude/DRY_RUN_OK`
- `Bash` de solo lectura (cat, grep, git diff, git log, pytest…) → aprueba
- `Bash` potencialmente mutante que matchee el PATTERN → bloquea sin marcador

**Si se renombra un módulo, hay que actualizar el PATTERN en el mismo commit.**
Un hook desincronizado es peor que no tener hook.

### Verificaciones de la sesión 1

- Auditoría directa del repo vía `raw.githubusercontent.com`
- CI real en GitHub Actions en verde
- Prueba funcional del hook, 5 pasos, todos con el resultado esperado

Hubo una primera versión del hook que bloqueaba también los comandos de solo
lectura. Se detectó y se corrigió: un hook que obliga a esquivarlo entrena al
operador a evadirlo.

---

## Sesión 2 — Contratos y modelo de datos

### Trabajo de campo previo

Antes de escribir nada se hizo ingeniería inversa de la API de 1x contra el
tráfico real y con consultas directas. Resultados relevantes:

- La API de resultados es pública: **no requiere ningún header de autenticación**
- `dateFrom` / `dateTo` deben ser **múltiplos de 300 segundos**, si no devuelve 400
- El `game_id` del LineFeed es **idéntico** al `id` de resultados → matching por
  entero exacto, se elimina la necesidad de fuzzy matching
- `liga.id` del LineFeed == `champId` de resultados → no hace falta tabla de
  equivalencia
- No existe consulta por partido individual: hay que agrupar por `champId`

Todo ello quedó documentado en `ESPECIFICACION_FUENTE.md`, separando lo
verificado de lo que sigue siendo supuesto.

### PRs de esta sesión

| PR | Contenido |
|---|---|
| #2 | `ESPECIFICACION_FUENTE.md`, regla 5, hook endurecido contra comandos encadenados (`&&`, `;`, `\|`) |
| #3 | `LIMITES.md`, regla 6, PATTERN ampliado con `limites`/`exposicion`/`banca`, exención de `.md` de raíz |
| #4 | PATTERN: `liquidacion` → `liquid`, más `settle`. Creación de este archivo con la sección de limitaciones |
| #5 | `REGLAS_LIQUIDACION.md`, regla 7, `SKILL.md` apuntando al documento canónico, exención de `.md` extendida a cualquier ruta |
| #6 | `ESQUEMA_DB.md`, `bot/db/migraciones/001_esquema_inicial.sql`, `bot/db/conversion.py`, 9 tests |

### Documentos de contrato en la raíz

| Documento | Qué fija |
|---|---|
| `ESPECIFICACION_FUENTE.md` | Endpoints de 1x, restricciones, lo verificado y lo que no |
| `LIMITES.md` | Bancas por moneda, exposición, combinadas, protección del usuario |
| `REGLAS_LIQUIDACION.md` | Resolución de los 14 mercados verificados, casos límite |
| `ESQUEMA_DB.md` | Modelo de datos, dinero en enteros, ledger, idempotencia |

### Bancas fijadas (Fase 1)

```
CUP  50.000
USD     100
```

Cripto queda fuera de Fase 1. Cada moneda es un libro contable independiente: no
se paga un premio con la banca de otra moneda.

### Iteraciones del hook sobre documentación

El gate disparó sobre archivos `.md` en tres ocasiones distintas antes de que la
regla quedara bien:

1. Bloqueaba `cat` y `git diff` → corregido en sesión 1
2. Bloqueaba `git add LIMITES.md` → exención de `.md` de raíz (PR #3)
3. Bloqueaba `.claude/skills/liquidacion/SKILL.md` → exención de `.md` en
   cualquier ruta (PR #5)

La regla correcta es estructural, no por directorio: **los `.md` son
especificación, no ejecutan nada ni tocan saldos, y no disparan el gate en
ninguna ruta.**

---

## Limitaciones conocidas

### El gate de Bash es heurístico

El gate de Bash matchea texto del comando, no rutas. Es heurístico y no puede ser
completo: un script con nombre no contemplado (`pagar.py`, `cerrar.py`) pasa el
filtro. La protección estructural real está en `Write`/`Edit`, que matchea rutas
bajo `bot/`. No confiar en el gate de Bash como única barrera antes de una
operación que toque saldos.

### DRY_RUN_OK no se usa para documentación

`DRY_RUN_OK` no se usa para editar documentación. Si el gate dispara sobre un
`.md`, es un fallo del PATTERN y se arregla el PATTERN, no se crea el marcador.

---

## Pendientes

### Railway

`bot/__main__.py` levanta `NotImplementedError`. Si hay un servicio de Railway
enganchado a este repo con auto-deploy desde `main`, **el deploy va a fallar al
arrancar**.

No es dañino (no hay DB de producción ni dinero en juego todavía), pero genera
ruido de alertas. Acción: comprobar si existe el servicio. Si existe, pausar el
auto-deploy hasta tener algo funcional. Si no existe, conectarlo recién cuando el
bot arranque de verdad.

### Verificaciones abiertas de la API

Listadas en `ESPECIFICACION_FUENTE.md` §13. Ninguna bloquea la Fase 1. Las dos
que se vuelven necesarias más adelante:

- Valor de `statisticInfo.status` al finalizar un partido → flag directo de
  finalización, necesario para live
- URL base de `bets_model_*.json` → catálogo completo de mercados, necesario para
  ampliar más allá de los 14 actuales

### Verificación pendiente de hándicap

La convención de signo de `T7`/`T8` en `REGLAS_LIQUIDACION.md` §5.3 está
**inferida del patrón de los datos, no verificada**. Antes de habilitar hándicap
en producción hay que comprobarla contra 3 partidos ya liquidados por 1x.

### Sin implementar

- `cliente_1x.py` y el resto de `fuente_resultados/`
- `cascada_fuentes.py`
- Motor de liquidación
- Capa Telegram
- Aceptación de apuestas y cálculo de exposición

---

## Metodología

El orden es diseño antes que código. El hook fuerza dry-run antes de tocar
settlement, así que la metodología está protegida por el sistema y no solo por
disciplina.

Los cuatro documentos de contrato son la referencia canónica. Si al implementar
aparece un supuesto que no está en ellos, se detiene, se verifica, y se añade al
documento antes de escribir el código que lo usa.
