# REGLAS_LIQUIDACION.md — Resolución de mercados

Contrato de liquidación para `bot/core/settlement_engine.py`.

**Alcance:** Fase 1. Solo `sportId = 1` (fútbol). Solo los 14 tipos verificados
en `ESPECIFICACION_FUENTE.md` §8.1.

> **Por qué existe este documento**
>
> La API de 1x entrega marcadores, no resultados de mercado. Devuelve
> `"2:1 (1:0,1:1)"`. La conversión de ese texto a *"¿ganó una apuesta a T10 con
> parámetro 2.5?"* la hace este código, tipo por tipo.
>
> Un error aquí **no falla ruidosamente**: paga mal, en silencio, a todo el
> mundo, hasta que alguien reclama. Ninguna regla de este documento se
> implementa sin su test.

---

## 1. Alcance y restricciones de Fase 1

| Restricción | Valor |
|---|---|
| Deporte | Solo `sportId = 1` (fútbol) |
| Tipos permitidos | Solo los de §5 |
| Parámetro `P` permitido | Solo múltiplos de 0,5 |
| Periodo | Solo tiempo reglamentario (90' + descuento) |

⚠️ **Estas reglas son específicas de fútbol.** En tenis no existe el empate, y
los totales son juegos o sets, no goles. Aplicar `T1`/`T2`/`T3` a `sportId = 4`
produce liquidaciones incorrectas. **Otros deportes necesitan su propio
documento.**

### Rechazo de líneas de cuarto

```python
def parametro_valido(p: Decimal | None) -> bool:
    """Solo líneas enteras o de medio punto. Los cuartos (asiáticas)
    parten el stake en dos y son fuente clásica de errores."""
    if p is None:
        return True
    return (p * 2) % 1 == 0
```

Líneas tipo `2.75`, `1.25`, `3.25` (observadas en la API) **se rechazan en Fase
1**. No se ofrecen, no se liquidan.

---

## 2. Estados de una selección

```python
class EstadoSeleccion(Enum):
    GANADA         = "ganada"
    PERDIDA        = "perdida"
    NULA           = "nula"            # push: cuota efectiva 1.00
    PENDIENTE      = "pendiente"
    REQUIERE_ADMIN = "requiere_admin"
```

- **NULA** no es "perdida". El stake de esa selección se devuelve. En una
  combinada, esa pata pasa a cuota **1,00** y las demás siguen su curso.
- **REQUIERE_ADMIN** nunca se resuelve solo. Sale de la cola automática.

---

## 3. Origen del marcador

Fuente única: campo `score` de `result/web/api/v3/games`, tras pasar
`parse_score()` (`ESPECIFICACION_FUENTE.md` §6).

```python
@dataclass(frozen=True)
class Marcador:
    local: int
    visitante: int
    periodos_raw: str
```

### Guardas previas — antes de resolver ningún mercado

```python
def marcador_utilizable(item: dict) -> Marcador | None:
    if not es_partido_real(item):          # ESPECIFICACION_FUENTE §5
        return None                        # → REQUIERE_ADMIN
    m = parse_score(item.get("score"))
    if m is None:
        return None                        # → REQUIERE_ADMIN
    return Marcador(**m)
```

⚠️ **`parse_score` devolviendo `None` nunca es 0:0.** Es `REQUIERE_ADMIN`.

### Guarda de prórroga

El formato `"2:1 (1:0,1:1)"` trae los parciales de los dos tiempos. Si aparecen
**más de dos periodos** en un partido de fútbol, es indicio de prórroga.

```python
def tiene_prorroga(m: Marcador) -> bool:
    if not m.periodos_raw:
        return False
    return len(m.periodos_raw.split(",")) > 2
```

⚠️ **La convención estándar es que 1X2, totales y hándicaps se resuelven al
final del tiempo reglamentario, sin prórroga ni penales.** Como no está
verificado si el campo `score` de 1x incluye la prórroga, cualquier partido con
`tiene_prorroga() == True` va a **REQUIERE_ADMIN** en Fase 1.

Es conservador a propósito: afecta solo a partidos de eliminatoria, y evita el
error más caro de esta categoría.

---

## 4. Firma de la función de resolución

```python
def resolver(tipo: int,
             parametro: Decimal | None,
             m: Marcador) -> EstadoSeleccion:
    ...
```

Sin efectos secundarios, sin acceso a base de datos, sin red. **Función pura.**
Es lo que la hace testeable exhaustivamente.

---

## 5. Reglas por mercado

### 5.1 Resultado 1X2 — sin parámetro

| `T` | Mercado | Gana si |
|---|---|---|
| 1 | **1** — local | `local > visitante` |
| 2 | **X** — empate | `local == visitante` |
| 3 | **2** — visitante | `visitante > local` |

```python
if tipo == 1:  return G if m.local > m.visitante else P
if tipo == 2:  return G if m.local == m.visitante else P
if tipo == 3:  return G if m.visitante > m.local else P
```

Nunca produce NULA. Los tres resultados son excluyentes y exhaustivos.

### 5.2 Doble oportunidad — sin parámetro

| `T` | Mercado | Gana si |
|---|---|---|
| 4 | **1X** | `local >= visitante` |
| 5 | **12** | `local != visitante` |
| 6 | **X2** | `visitante >= local` |

```python
if tipo == 4:  return G if m.local >= m.visitante else P
if tipo == 5:  return G if m.local != m.visitante else P
if tipo == 6:  return G if m.visitante >= m.local else P
```

Nunca produce NULA.

### 5.3 Hándicap — requiere parámetro

| `T` | Mercado |
|---|---|
| 7 | Hándicap **local**, `P` se suma al marcador del local |
| 8 | Hándicap **visitante**, `P` se suma al marcador del visitante |

```python
if tipo == 7:
    dif = (m.local + parametro) - m.visitante
elif tipo == 8:
    dif = (m.visitante + parametro) - m.local

if dif > 0:   return GANADA
if dif == 0:  return NULA          # push, solo posible con P entero
return PERDIDA
```

⚠️ **Convención de signo INFERIDA, no verificada.** Se dedujo del patrón de los
datos: en Inter (favorito, cuota 1.264) vs Monza aparece `T7 P=-1.5`, y en Hull
(no favorito, cuota 7.42) vs Man Utd aparece `T7 P=+1.5`. Consistente con
"`P` se aplica al equipo del propio mercado".

**Antes de habilitar hándicap en producción hay que verificarlo** contra al
menos 3 partidos ya liquidados por 1x, comparando el resultado calculado con el
que 1x pagó realmente.

#### Ejemplos

```
Marcador 2:0

T7  P=-1.5  →  (2-1.5)-0 = +0.5  →  GANADA
T7  P=-2.0  →  (2-2.0)-0 =  0.0  →  NULA      ← push
T7  P=-2.5  →  (2-2.5)-0 = -0.5  →  PERDIDA
T8  P=+1.5  →  (0+1.5)-2 = -0.5  →  PERDIDA
T8  P=+2.0  →  (0+2.0)-2 =  0.0  →  NULA
```

### 5.4 Total de goles — requiere parámetro

| `T` | Mercado |
|---|---|
| 9 | Total **más de** `P` |
| 10 | Total **menos de** `P` |

```python
total = m.local + m.visitante

if tipo == 9:
    if total > parametro:   return GANADA
    if total == parametro:  return NULA
    return PERDIDA

if tipo == 10:
    if total < parametro:   return GANADA
    if total == parametro:  return NULA
    return PERDIDA
```

#### Ejemplos

```
Marcador 1:1  → total 2

T9   P=1.5  →  GANADA
T9   P=2.0  →  NULA        ← push, línea exacta
T9   P=2.5  →  PERDIDA
T10  P=2.5  →  GANADA
T10  P=2.0  →  NULA
```

### 5.5 Total individual — requiere parámetro

| `T` | Mercado |
|---|---|
| 11 | Total **local**, más de `P` |
| 12 | Total **local**, menos de `P` |
| 13 | Total **visitante**, más de `P` |
| 14 | Total **visitante**, menos de `P` |

```python
equipo = m.local if tipo in (11, 12) else m.visitante
mas    = tipo in (11, 13)

if equipo == parametro:  return NULA
if mas:  return GANADA if equipo > parametro else PERDIDA
else:    return GANADA if equipo < parametro else PERDIDA
```

⚠️ **`T11`/`T12` son del local y `T13`/`T14` del visitante.** Invertirlo es un
error silencioso que solo se nota cuando el marcador es asimétrico. Test
obligatorio con marcador **no simétrico** (§8).

### 5.6 Ambos equipos marcan — sin parámetro

| `T` | Mercado | Gana si |
|---|---|---|
| 180 | **Sí** | `local >= 1 and visitante >= 1` |
| 181 | **No** | `local == 0 or visitante == 0` |

```python
ambos = m.local >= 1 and m.visitante >= 1
if tipo == 180:  return GANADA if ambos else PERDIDA
if tipo == 181:  return PERDIDA if ambos else GANADA
```

Nunca produce NULA. `0:0` → `T180` PERDIDA, `T181` GANADA.

### 5.7 Qué tipos requieren parámetro

Está implícito en la descripción de cada mercado de arriba; se deja explícito
acá porque un `parametro` ausente o presente donde no corresponde es un bug
del llamador, no un caso de negocio (§9 — `resolver()` lo rechaza con
`ValueError`).

| Requiere parámetro | Tipos |
|---|---|
| Sí | 7, 8, 9, 10, 11, 12, 13, 14 |
| No | 1, 2, 3, 4, 5, 6, 180, 181 |

---

## 6. Liquidación de combinadas

Regla base: **la combinada gana solo si todas sus patas ganan.** Basta una
PERDIDA para que el ticket entero pierda.

Las patas NULAS son la parte delicada.

```python
def resolver_combinada(patas) -> tuple[EstadoSeleccion, Decimal]:
    """Devuelve (estado, cuota_efectiva)."""
    estados = [p.estado for p in patas]

    if EstadoSeleccion.REQUIERE_ADMIN in estados:
        return EstadoSeleccion.REQUIERE_ADMIN, Decimal(0)

    if EstadoSeleccion.PERDIDA in estados:
        return EstadoSeleccion.PERDIDA, Decimal(0)

    if EstadoSeleccion.PENDIENTE in estados:
        return EstadoSeleccion.PENDIENTE, Decimal(0)

    # Solo quedan GANADA y NULA.
    # Una pata NULA aporta cuota 1.00: no suma, pero tampoco anula el ticket.
    cuota = Decimal(1)
    for p in patas:
        if p.estado == EstadoSeleccion.GANADA:
            cuota *= p.cuota
    return EstadoSeleccion.GANADA, cuota
```

⚠️ **Corrección (versión anterior tenía PENDIENTE antes que PERDIDA — era un
error del documento).** El orden correcto es
**REQUIERE_ADMIN → PERDIDA → PENDIENTE → GANADA**.

**Por qué PERDIDA va antes que PENDIENTE:** una pata perdida condena el ticket
matemáticamente — ninguna otra pata pendiente puede revertirlo. Esperar solo
retiene exposición sobre algo que ya no puede pagar (`LIMITES.md` §4). No hay
motivo para no cerrarlo ya.

**Por qué REQUIERE_ADMIN sigue yendo primero, incluso antes que PERDIDA:** una
anomalía (dato roto, partido combinado, prórroga no verificada) puede tener la
misma causa raíz que la pata marcada perdida — si el dato de origen está mal,
"perdida" también podría estar mal. Una espera normal (PENDIENTE) no tiene ese
riesgo: es simplemente un partido que no terminó. Por eso PERDIDA sí le gana a
PENDIENTE, pero no le gana a REQUIERE_ADMIN.

### Caso extremo: todas las patas NULAS

`cuota = 1.00` → se devuelve el stake íntegro. Correcto y esperado.

---

## 7. Aritmética y redondeo

```python
from decimal import Decimal, ROUND_HALF_UP
```

⚠️ **Nunca `float` para dinero.** `0.1 + 0.2 != 0.3` y en un libro de apuestas
eso se acumula.

```python
CENTIMOS = Decimal("0.01")

def calcular_payout(stake: Decimal, cuota: Decimal) -> Decimal:
    return (stake * cuota).quantize(CENTIMOS, rounding=ROUND_HALF_UP)
```

🔴 **La misma función se usa en los dos sitios:** al mostrar el payout potencial
antes de que el usuario confirme, y al liquidar. Si son dos funciones distintas,
tarde o temprano difieren y el usuario cobra algo distinto de lo que le
prometiste.

Es motivo de queja legítima y de pérdida de confianza inmediata.

### 7.1 Redondeo de combinadas: dos campos, dos momentos

El producto de varias cuotas de 3 decimales acumula más de 3 decimales
(4 patas → hasta 12). Hay que decidir dónde se redondea, y la decisión es:
**la cuota efectiva de una combinada se redondea a milésimas antes de
calcular el payout** — no se acumula en precisión completa y se redondea
recién al final.

```python
MIL = Decimal("1000")

def cuota_efectiva_milesimas(cuotas_ganadas_milesimas: list[int]) -> int:
    producto = Decimal(1)
    for c in cuotas_ganadas_milesimas:
        producto *= Decimal(c) / MIL
    return int((producto * MIL).to_integral_value(ROUND_HALF_UP))

def payout_combinada_cent(stake_cent: int, cuota_efectiva_milesimas: int) -> int:
    return int(
        (Decimal(stake_cent) * Decimal(cuota_efectiva_milesimas) / MIL)
        .to_integral_value(ROUND_HALF_UP)
    )
```

**Por qué acá y no al final:** `tickets.cuota_milesimas` (`ESQUEMA_DB.md`) es
`INTEGER` — no hay dónde guardar 12 decimales. El payout que se le muestra al
usuario **antes de confirmar** (`payout_pot_cent`) sale de multiplicar las
cuotas de las patas y redondear a milésimas, porque es la única forma de que
exista un entero para esa columna. Si la liquidación usara la cuota sin
redondear, el payout final podría diferir en el último centavo del que se le
prometió — exactamente el problema que el punto 🔴 de arriba ya prohíbe.
Redondeando a milésimas en el mismo punto en que `core/apuestas.py` tiene que
redondear para persistir, los dos cálculos dan siempre el mismo resultado,
porque las cuotas de las patas están congeladas (§1.3) desde la aceptación.

⚠️ **`cuota_milesimas` y `cuota_efectiva_milesimas` son dos campos distintos,
a propósito:**

| Campo | Cuándo se calcula | Qué asume |
|---|---|---|
| `tickets.cuota_milesimas` | Al aceptar la apuesta | **Todas** las patas ganan — es la cuota "de catálogo" del combo, la que decide `payout_pot_cent` y la exposición (`LIMITES.md` §4) |
| `cuota_efectiva_milesimas` (liquidación) | Al liquidar | Puede ser **menor** si alguna pata quedó NULA (esa pata aporta 1.000 en vez de su cuota real) |

Son iguales cuando no hay patas NULAS, y `cuota_efectiva_milesimas <=
cuota_milesimas` siempre. Tratarlos como el mismo campo pagaría de más
cualquier combinada con una pata nula.

---

## 8. Tests obligatorios

Ninguna regla se mergea sin su test. Mínimo por mercado:

### Por cada tipo

| Caso | Debe cubrir |
|---|---|
| Victoria local | `2:0` |
| Victoria visitante | `0:2` |
| Empate | `1:1` |
| Sin goles | `0:0` |
| **Asimétrico** | `3:1` — detecta local/visitante invertidos |

### Casos de push

```
T7  P entero, diferencia exacta      → NULA
T9  P entero, total exacto           → NULA
T10 P entero, total exacto           → NULA
T11 P entero, goles exactos          → NULA
```

### Casos de guarda

```
score = "texto raro"       → parse_score None → REQUIERE_ADMIN
score = ""                 → REQUIERE_ADMIN
item con opp1Ids de 2      → REQUIERE_ADMIN
item con dopInfo no vacío  → REQUIERE_ADMIN
3 periodos en periodos_raw → REQUIERE_ADMIN
P = 2.75                   → rechazo antes de aceptar la apuesta
```

### Combinadas

```
todas ganadas                    → producto de cuotas
una perdida                      → PERDIDA
una nula + resto ganadas         → producto sin la nula
todas nulas                      → cuota 1.00, devolución
una pendiente                    → PENDIENTE
una requiere_admin               → REQUIERE_ADMIN
```

---

## 9. Mercados prohibidos en Fase 1

No se ofrecen y, si aparecen en el feed, se filtran antes de mostrarse:

- Cualquier `T` fuera de §5
- Cualquier `P` que no sea múltiplo de 0,5
- Cualquier `sportId != 1`
- Mercados de `subGame` (córners, tarjetas, tiros) — el feed los trae, pero
  necesitan sus propias reglas
- Mercados con `"Resultados combinados"` — el campo `score` no es numérico
  (`"Goles: 2 Saques de esquina: 8 Tarjetas: 5"`)

⚠️ El filtro va **en la capa de presentación**. Si el usuario no lo puede ver,
no lo puede apostar, y nunca llega al liquidador.

---

## 10. Partidos suspendidos, aplazados o abandonados

Si el partido no aparece en `v3/games` tras el plazo esperado:

```
→ NO_CONFIRMADO
→ nunca se liquida solo
→ tras 24 h sin resultado: REQUIERE_ADMIN + alerta
```

Un partido abandonado al minuto 70 puede aparecer en resultados **con el
marcador parcial y sin ninguna señal de que se abandonó**. La guarda de estabilidad
de marcador (`ESPECIFICACION_FUENTE.md` §11) no lo detecta: un marcador
abandonado también es estable.

❌ **No hay forma verificada de detectar abandono desde esta API.** Es un riesgo
asumido y conocido de Fase 1. Mitigación práctica: revisión manual de los
partidos cuyo resultado difiera de lo esperado, hasta encontrar un campo que lo
señale.

### Heurística parcial: exactamente 1 periodo en `periodos_raw`

`cascada_fuentes.py` cierra **parcialmente** este hueco. Un partido de fútbol
terminado normalmente trae 2 periodos en `periodos_raw` (primer y segundo
tiempo). Si trae exactamente 1, es la firma de un abandono **antes de que
empiece o durante el primer tiempo** — nunca se generó el segundo periodo — y
se manda a `REQUIERE_ADMIN` con motivo `"un solo periodo, posible partido
abandonado"`.

⚠️ **Es una heurística estructural, no una señal del proveedor.** No cubre el
caso original de este apartado: un abandono en el minuto 70 u 80 ocurre
**durante** el segundo tiempo, así que `periodos_raw` ya tiene sus 2 periodos
(el primero completo, el segundo parcial) y pasa esta guarda sin disparar
nada. Ese abandono **sigue sin detectarse**.

`periodos_raw` vacío (0 periodos, sin desglose) no dispara esta guarda: nunca
se observó ese caso en la API real, así que no hay base verificada para
decidir qué significa (ver `cascada_fuentes.py`, `_tiene_un_solo_periodo`).

---

## 11. No verificado

| # | Punto | Riesgo |
|---|---|---|
| 1 | Convención de signo del hándicap (§5.3) | Liquidación invertida de T7/T8 |
| 2 | Si `score` incluye prórroga | Mitigado por la guarda de §3 |
| 3 | Detección de partido abandonado | §10 |
| 4 | Si 1x anula mercados por eventos especiales (gol anulado por VAR tras el pitido, etc.) | Discrepancia con lo que pagó 1x |

**La #1 se verifica antes de habilitar hándicap.** Método: tomar 3 partidos ya
liquidados, calcular con estas reglas, comparar con lo que 1x pagó. Si coincide
en los 3, se marca verificado y se actualiza este documento.
