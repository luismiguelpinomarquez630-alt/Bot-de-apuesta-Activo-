# LIMITES.md — Límites de riesgo y exposición

Contrato de riesgo para `bot/core/`. Todo límite aquí definido es **una constante
verificable en código**, no una guía.

**Estado:** valores fijados. Implementable.

---

## 0. Advertencia sobre estos números

Los porcentajes son heurísticas convencionales de gestión de riesgo, no valores
optimizados para esta operación concreta. Son un punto de partida
deliberadamente conservador.

Independientemente de los números elegidos, es obligatorio:

- Todo límite se comprueba **antes** de aceptar la apuesta, no después.
- Todo límite vive en configuración, **nunca hardcodeado**.
- Todo límite tiene un test que verifica que se rechaza al superarlo.

---

## 1. Bancas — un libro por moneda

⚠️ **Cada moneda es un libro contable independiente.** No es un campo `moneda`
en una tabla: es banca propia, exposición propia y conciliación propia.

```python
BANCAS = {
    "CUP": Decimal("50000"),
    "USD": Decimal("100"),
}
```

### Regla dura de no convertibilidad

**Un premio se paga en la misma moneda en que se aceptó la apuesta, con la banca
de esa moneda.** Nunca se paga un premio en USD con banca en CUP, ni al revés.

Cruzar monedas añade riesgo de tipo de cambio encima del riesgo de apuestas. Con
estas bancas, una variación del cambio descoloca los límites enteros.

### Definición de banca

`BANCA` es **dinero propio, líquido, que puedes perder entero**. No es el saldo
en caja.

```
BANCA = efectivo disponible − saldos que los usuarios tienen en el bot
```

El dinero que depositó un usuario **no es tu banca**: es deuda tuya y se lo
puede retirar cuando quiera.

---

## 2. Valores derivados

| Límite | % banca | CUP (50.000) | USD (100) |
|---|---|---|---|
| `STAKE_MAX_SIMPLE` | 1 % | **500** | 1,00 |
| `PAYOUT_MAX_TICKET` | 5 % | **2.500** | 5,00 |
| `EXPOSICION_MAX_EVENTO` | 10 % | **5.000** | 10,00 |
| `EXPOSICION_MAX_GLOBAL` | 30 % | **15.000** | 30,00 |
| `STAKE_DIARIO_MAX_USUARIO` | 4 % | **2.000** | 4,00 |
| `STAKE_MIN` | — | **20** | 0,50 |

| Límite | Valor (ambas monedas) |
|---|---|
| `COMBI_MAX_PATAS` | **4** |
| `COMBI_CUOTA_MAX` | **20,0** |
| `CUOTA_MAX_ACEPTADA` | **20,0** |

⚠️ `COMBI_CUOTA_MAX` no puede ser 50: con stake 500 daría un payout de 25.000,
la mitad de la banca en un solo ticket.

### Techo implícito de operación

Con 15.000 CUP de exposición global y un payout medio de ~700 por ticket, el
sistema soporta **unos 20 tickets abiertos simultáneamente**. Ese es el límite
real de usuarios concurrentes en Fase 1.

### ⚠️ Nota sobre el libro USD

Con banca de 100 USD, el stake máximo es **1,00 USD**. Eso no es un producto
comercial, es una moneda de prueba.

Dos opciones honestas:

1. **Abrir USD solo cuando la banca llegue a 300 USD** → stake máx 3, payout máx
   15. Sigue siendo pequeño pero usable.
2. **Abrirlo ya**, asumiendo que USD funciona como validación técnica del código
   multi-moneda, no como fuente de ingresos.

Lo que **no** es opción: subir los porcentajes solo para el libro USD porque los
números salen chicos. Ahí es donde se pierde la capacidad de pagar.

---

## 3. Límite por ticket

```
payout_potencial = stake * cuota_total

si payout_potencial > PAYOUT_MAX_TICKET[moneda]:
    → rechazar, O reducir stake automáticamente hasta el tope
```

⚠️ **Si se reduce el stake, hay que mostrárselo al usuario ANTES de que
confirme.** Aceptar 500 y liquidar como si fueran 300 es una queja garantizada y
con razón.

### Ejemplo (CUP)

```
Apuesta 500 a cuota 1.80  → payout    900   ✅ acepta
Apuesta 500 a cuota 12.0  → payout  6.000   ❌ excede 2.500
                          → stake máximo permitido: 208
```

---

## 4. Exposición por evento

El límite más importante y el que más se olvida.

```
EXPOSICION_MAX_EVENTO = 10 % de la banca de esa moneda
```

**Definición:** suma del payout potencial de *todas* las apuestas abiertas cuyo
resultado dependa de un mismo `game_id`, **incluidas las patas de combinadas**.

### Por qué las combinadas cuentan

Si 20 usuarios llevan "gana el Real Madrid" dentro de combinadas distintas y el
Madrid gana, **pagas las 20**. Contar solo las apuestas simples subestima la
exposición real, y es exactamente así como se revienta una banca.

### Cálculo del peor caso

```python
def exposicion_evento(game_id, moneda) -> Decimal:
    """Peor caso: el resultado que más obliga a pagar."""
    escenarios = defaultdict(Decimal)
    for ap in apuestas_abiertas_que_tocan(game_id, moneda):
        for resultado in resultados_posibles(game_id):
            if ap.gana_si(resultado):
                escenarios[resultado] += ap.payout_potencial
    return max(escenarios.values(), default=Decimal(0))
```

⚠️ Para combinadas, `payout_potencial` es el del **ticket completo**, no la parte
proporcional de esa pata.

### Implementación elegida para Fase 1: sobre-cálculo

El cálculo exacto de arriba enumera escenarios y depende de
`resultados_posibles(game_id)` — eso no existe hoy y acoplaría este límite a
`resolver()` y al marcador real. Fase 1 usa una aproximación más simple,
implementada en `bot/core/exposicion.py`:

```
exposicion_evento(game_id, moneda) = suma de payout_pot_cent de TODOS los
tickets pendientes con una selección pendiente en ese game_id, en esa moneda.
```

Es un límite superior del peor caso real: asume que todas las apuestas del
evento ganan a la vez, aunque algunas sean excluyentes entre sí ("gana Local" y
"gana Visitante" del mismo partido nunca ganan juntas, pero esta suma las
cuenta como si pudieran). Se elige a propósito:

- nunca subestima el riesgo — es el lado seguro de un límite
- es O(n), una suma, sin enumerar resultados
- no depende de `resolver()` ni de conocer el marcador

El cálculo exacto por escenarios queda pendiente para más adelante, si hiciera
falta afinar el límite y dejar de suspender eventos antes de lo estrictamente
necesario.

### Al superarse

```
suspender el evento para nuevas apuestas
alertar al admin
NO cancelar las ya aceptadas
```

---

## 5. Exposición global

```
EXPOSICION_MAX_GLOBAL = 30 % de la banca de esa moneda
```

Suma del peor caso de todos los eventos abiertos, **por moneda**. Al superarse:
no se aceptan apuestas nuevas en esa moneda hasta liquidar algo.

Es el freno de emergencia. Si se activa con frecuencia, los límites por evento
están mal calibrados.

---

## 6. 🔴 Solvencia — regla de parada de depósitos

```
si suma_saldos_usuarios(moneda) > BANCA[moneda]:
    → detener aceptación de depósitos en esa moneda
    → alertar al admin
```

Con 50.000 CUP de banca, un puñado de depósitos grandes puede superarla. A
partir de ese punto **estás operando con dinero de los usuarios, no con el
tuyo**: si algo sale mal, el que pierde es quien depositó.

En una comunidad pequeña y por Telegram, un impago se sabe en un día y no se
remonta.

Esta comprobación corre en cada depósito, no en un cron.

---

## 7. Combinadas

| Límite | Valor |
|---|---|
| `COMBI_MAX_PATAS` | **4** |
| `COMBI_CUOTA_MAX` | **20,0** |
| `COMBI_MISMO_PARTIDO` | **prohibido** |
| `COMBI_MISMA_MONEDA` | **obligatorio** |

### Regla de correlación

⚠️ **Una sola selección por `game_id` dentro de una combinada.**

"Gana Local" y "Local marca +1.5" **no son eventos independientes**. Multiplicar
sus cuotas regala valor esperado: el usuario compra algo mucho más probable de
lo que la cuota combinada refleja.

Es el error más caro y más fácil de cometer en un libro nuevo.

La alternativa correcta (mercados combinados con precio propio, tipo *Bet
Builder*) es un producto distinto y mucho más complejo. **Fuera de alcance.**

### Validación

```python
def combinada_valida(selecciones, moneda) -> bool:
    if not 2 <= len(selecciones) <= COMBI_MAX_PATAS:
        return False
    ids = [s.game_id for s in selecciones]
    if len(ids) != len(set(ids)):
        return False                      # dos patas del mismo partido
    if any(s.moneda != moneda for s in selecciones):
        return False                      # monedas mezcladas
    if cuota_total(selecciones) > COMBI_CUOTA_MAX:
        return False
    return True
```

---

## 8. Límites por usuario

### Protección de la banca

| Límite | CUP | USD |
|---|---|---|
| `STAKE_DIARIO_MAX_USUARIO` | 2.000 | 4,00 |
| `GANANCIA_NETA_DIARIA_ALERTA` | 1.500 | 3,00 |

La alerta **no bloquea**. Un usuario que gana de forma sostenida no es
sospechoso por ganar: puede tener mejor información, sobre todo en vivo (§10).
La alerta existe para que lo mires, no para castigarlo automáticamente.

### Protección del usuario

No es opcional ni decorativa. Un bot de apuestas sin límites de usuario facilita
comportamiento adictivo, y el daño recae sobre gente real y cercana.

| Mecanismo | Comportamiento |
|---|---|
| `DEPOSITO_DIARIO_MAX` | Configurable **por el propio usuario**. Bajarlo tiene efecto inmediato; subirlo requiere 24 h de espera |
| Autoexclusión | Comando que bloquea la cuenta 7 / 30 / 90 días o permanente. **Irreversible dentro del plazo elegido** |
| Alerta de patrón | Si el stake diario sube >300 % respecto a la media de 30 días del usuario, aviso antes de aceptar más |
| Sin persecución | Un usuario autoexcluido o inactivo **no recibe ningún mensaje promocional** |

⚠️ Activar la autoexclusión y bajar el tope tienen que ser **más fáciles que
revertirlos**. Si están enterrados en un submenú, no cumplen su función.

---

## 9. Momento y atomicidad

Todo lo anterior es inútil si se comprueba en el momento equivocado.

```
1. Usuario selecciona y envía stake
2. ── BEGIN IMMEDIATE ──
   a. Bloquear fila de saldo del usuario
   b. Verificar saldo suficiente en esa moneda
   c. Recalcular exposición del evento CON esta apuesta incluida
   d. Verificar TODOS los límites de esa moneda
   e. Si algo falla → rollback + mensaje al usuario
   f. Debitar saldo + insertar apuesta
   ── COMMIT ──
3. Confirmar al usuario
```

⚠️ Los pasos c–f van en **la misma transacción**. Si la exposición se comprueba
fuera, dos apuestas simultáneas pasan las dos y el límite se salta.

SQLite en WAL lo soporta, pero hay que usar `BEGIN IMMEDIATE`, no el modo
diferido por defecto.

---

## 10. Live — riesgo estructural no cubierto aquí

Este documento **no cubre el riesgo de latencia**, que es el riesgo dominante en
apuestas en vivo.

El problema: el feed consulta a 1x con retraso. Un usuario mirando 1x
directamente ve el gol antes que el bot y apuesta a una cuota que ya no es
válida. **No lo detiene ningún límite de exposición.**

Mitigaciones mínimas antes de habilitar live:

- Suspensión automática de mercados al detectar cambio de marcador o de cuota
- Delay de aceptación (5–10 s) con **reconfirmación de cuota** antes de sellar
- Margen propio sobre la cuota de 1x, no copiarla al pelo
- Límites de stake en vivo **más bajos** que en prepartido

**Live no se habilita hasta que esto esté implementado y probado.** Documento
aparte: `LIMITES_LIVE.md`, en Fase 3.

---

## 11. Cripto — Fase 2, con condición de solvencia

Cripto **no entra en Fase 1**.

Cuando entre, aplica la misma regla que cualquier otra moneda, y una condición
adicional que la hace o la rompe:

⚠️ **Si aceptas apuestas en cripto, tienes que poder pagar `PAYOUT_MAX_TICKET`
en cripto, hoy, sin depender de que otro usuario deposite.**

El modelo "pago los premios en cripto solo con lo que me depositen en cripto"
**es insolvencia por diseño**:

```
Usuario deposita   50 USDT
Apuesta            20 USDT a cuota 5.0
Gana            →  debes 100 USDT
En caja            50 USDT
```

No es un problema de liquidez pasajero: el bot aceptó un riesgo que la banca no
respaldaba.

O hay banca cripto real y separada, o no se ofrece cripto. No hay punto medio.

**Solo stablecoin** (USDT / USDC). Con BTC el valor se mueve entre aceptar y
liquidar, y eso es un tercer riesgo encima de los otros dos.

---

## 12. Pendiente de decisión

| # | Decisión | Bloquea |
|---|---|---|
| 1 | ¿Abrir USD con 100 o esperar a 300? | §2 |
| 2 | ¿Reducir stake automáticamente o rechazar al superar payout? | §3 |
| 3 | Margen propio sobre la cuota de 1x | §10 y el precio de todo |
| 4 | Banca cripto real | §11, Fase 2 |

Ninguna bloquea la implementación en CUP.
