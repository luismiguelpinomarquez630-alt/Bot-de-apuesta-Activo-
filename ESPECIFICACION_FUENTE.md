# ESPECIFICACION_FUENTE.md — API 1x

Contrato de la fuente de datos para `bot/fuente_resultados/`.

**Fecha de verificación:** 2026-07-24
**Método:** captura de tráfico real + consultas directas al endpoint.

> **Regla de uso de este documento**
>
> Todo lo marcado ✅ está comprobado contra la API real y puede implementarse.
> Todo lo marcado ❌ **no está verificado**. No implementar lógica que dependa
> de ello sin comprobarlo primero. Si al implementar aparece un supuesto que no
> está en este documento, es un supuesto no verificado: hay que comprobarlo y
> añadirlo aquí antes de escribir el código que lo usa.

---

## 0. Fuente de producción ✅

⚠️ **`bol.1xbet.com` NO sirve desde datacenter.** Devuelve 403 a las
peticiones que salen de Railway, aunque el mismo endpoint responde 200 desde
un navegador en Cuba — es un bloqueo por IP/ASN del lado de 1x, no un
problema de parámetros ni de headers. Verificado con un diagnóstico
temporal corrido en Railway (PRs #18-#25, ya revertido).

**Fuente real de producción: `https://provider.betfantasy.bet`.**

| Concepto | Valor |
|---|---|
| Responde 200 desde datacenter | ✅ Verificado en Railway |
| Autenticación | ✅ Ninguna, igual que `bol.1xbet.com` |
| Formato de respuesta | ✅ Idéntico a 1x: sobre `{Success, Value}` en `LineFeed/*Zip`, esquema `_VZip` (§7: `I`, `E[]`, `T`, `C`, `CV`, `P`, `G`, `CE`), `count`/`items` en los endpoints de `/result/` |
| Resultados (`v2/champs`, `v3/games`) | ✅ Mismos parámetros que 1x: `lng=es`, `ref=156` (sin `country`/`partner`) |
| Cuotas (`LineFeed/Get1x2_VZip`) | ✅ Verificado contra la URL real de la app (Network): `lng=es`, `mode=4`, `country=71`, `partner=188`, `virtualSports=true`, `getEmpty=true`, `countryFirst=true` — **sin `gr` ni `cfview`, la app no los manda** |

⚠️ **`Get1x2_VZip` EXIGE `champs=<champ_id>`. No permite barrer un deporte
entero de una — a diferencia de 1x.** Esta fue la causa real de que el
primer intento devolviera `Value` vacío (y, con `count` alto, 502 por
Cloudflare): faltaba el filtro por liga, no alcanza con `sports`. Flujo de
dos pasos, obligatorio:

1. **`LiveFeed/WebGetTopChampsZip`** (`lng=es`, `country=71`, `partner=188`,
   sin más parámetros) → lista de ligas. Campo del id de liga: `LI`. Devuelve
   un set **reducido** ("top" leagues) — verificado que ya viene sin ligas
   sintéticas, así que sirve como whitelist natural: a diferencia de
   `v2/champs` (que sí mezcla simuladas, §10), acá no hace falta armar una
   lista manual. El filtro por deporte es del lado del cliente, sobre el
   campo `SI` de cada item — **verificado contra el JSON real:** `SI=1`
   trae `SN="Fútbol"` (mismo campo que en el esquema `_VZip`).
2. **`LineFeed/Get1x2_VZip`** por cada liga del paso 1, con `champs=<LI>`
   sumado a los parámetros de cuotas de la tabla de arriba.

⚠️ **Nota de operación: el set de `WebGetTopChampsZip` es chico a
propósito** (header `live-top-leagues` — puede ser 1-5 ligas, no decenas).
Un refresco de cuotas que solo trae fútbol de un puñado de ligas es
**comportamiento esperado, no un bug** del flujo de dos pasos. Pendiente de
observar en producción: si esa cobertura de "top leagues" alcanza para
ofrecer suficiente fútbol prepartido, o si hace falta buscar otra vía para
ampliarla.

Implementado en `bot/fuente_resultados/primaria/cliente_1x.py`
(`obtener_ligas_con_cuotas` + `obtener_cuotas(sport_id, champ_id, count)`) y
orquestado en `bot/fuente_resultados/cache_cuotas.py::refrescar()`:
secuencial (no concurrente — no tiene sentido golpear a provider con N
pedidos en paralelo justo después de haber visto que Cloudflare corta
pedidos grandes con 502) y todo-o-nada (si falla el paso 1 o CUALQUIER
liga del paso 2, se aborta el refresco entero y se conserva el snapshot
anterior completo, nunca uno a medias).

⚠️ **`Get1x2_VZip` es lento y pesado contra provider: 4-10s, 100+KB por
liga.** Con `sports` filtrado (un solo deporte) baja a la mitad: 52KB/2.44s
medido, contra 128KB/4.25s sin filtrar. `count` alto (1000) hace que
Cloudflare corte con 502 antes de que provider arme la respuesta — la app
real usa counts mucho menores. Consecuencias, ya aplicadas:

- `cliente_1x.TIMEOUT_S` subido de 10s a 30s.
- `cliente_1x.obtener_cuotas()` **siempre** filtra por `sports` y por
  `champs` (obligatorio, ver arriba).
- Default de `count` bajado de 1000 a 50 (`BOT_COUNT_CUOTAS`).
- `cache_cuotas.INTERVALO_REFRESCO_S` subido de 15s a 45s y
  `cache_cuotas.TTL_S` de 30s a 90s (`CACHE_CUOTAS.md §6-§7`), para que un
  ciclo lento (ahora con N ligas en secuencia) no solape con el siguiente
  ni deje el snapshot vencido.
- 502 esporádicos (no crónicos) de Cloudflare: `cliente_1x._get()` ya los
  reintenta (`status >= 500`), sin cambios — la responsabilidad es de la
  capa de reintentos existente, no de código nuevo.

⚠️ **`provider_request_busy`: se detecta por el BODY, no por el status.**
Observado con `200`, `409` y `429` indistintamente — adivinar el status es
más frágil que mirar el contenido. `cliente_1x._get()` parsea el body antes
de `raise_for_status()` y, si trae `{"error": "provider_request_busy"}`, lo
trata como transitorio y reintenta con el mismo backoff que un 5xx.
`BACKOFF_BASE_S` subido de 1s a 3s (misma fórmula `* 2**intento`: 3s, 6s,
12s) para dar más margen a que la petición anterior termine en provider
antes de reintentar la idéntica.

`bol.1xbet.com` queda como referencia de formato y de la ingeniería inversa
original (§1 en adelante la sigue documentando), pero el cliente apunta a
`provider.betfantasy.bet`.

---

## 1. Host y autenticación

| Concepto | Valor |
|---|---|
| Host principal | `https://bol.1xbet.com` |
| Espejo observado | `https://1x-bet.mobi` (server Angie, sin Cloudflare) |
| Autenticación | ✅ **Ninguna.** No requiere cookies ni tokens |
| Header `x-hd` | ✅ **No obligatorio.** Consultas sin ningún header devuelven 200 |

Los parámetros `ref`, `partner`, `gr` y `country` identifican al operador y a la
región. En el host principal se observó `ref=156 / partner=156 / gr=687 /
country=97`. En el espejo, `ref=1 / gr=455`.

❌ **No verificado:** si el espejo requiere `x-hd`. Las consultas sin headers
contra `1x-bet.mobi` devolvieron 400. No se pudo distinguir entre "requiere
headers" y "el `gameId` consultado ya no estaba en el feed en vivo".

---

## 2. Restricción crítica de timestamps

✅ **`dateFrom` y `dateTo` deben ser múltiplos exactos de 300 segundos.**

Un timestamp no alineado produce **HTTP 400** con este cuerpo:

```json
{"type":"https://company.com/feeds/result/bad-request",
 "title":"Bad request","status":400,
 "detail":"Error occurred during request execution. Contact the developer.",
 "errorCode":1}
```

Evidencia:

| Timestamp | ÷300 | Resultado |
|---|---|---|
| 1784791200 | exacto | ✅ 200 |
| 1784792100 | exacto | ✅ 200 |
| 1784878500 | exacto | ✅ 200 |
| 1784880000 | exacto | ✅ 200 |
| 1784803600 | 5949345.33 | ❌ 400 |
| 1784890000 | 5949633.33 | ❌ 400 |

Implementación obligatoria en el cliente:

```python
def alinear_ts(ts: int) -> int:
    """Los endpoints de resultados exigen múltiplos de 300 s."""
    return (ts // 300) * 300
```

❌ **No verificado:** el tamaño máximo de la ventana. Todas las consultas
exitosas usaron exactamente 86400 s. El único intento con ventana mayor
(200000 s) falló, pero sus timestamps además estaban desalineados, así que el
fallo no es atribuible al tamaño. **Hasta comprobarlo, usar ventanas de 86400 s.**

---

## 3. Endpoints

### 3.1 Resultados — ligas con resultados ✅

```
GET /service-api/result/web/api/v2/champs
    ?dateFrom={ts}&dateTo={ts}&lng=es&ref=156&sportIds={sportId}
```

⚠️ Versión **v2**. `cache-control: public, max-age=60`.

```json
{"count":141,"items":[
  {"id":119599,"name":"Argentina. Liga Profesional","sportId":1,"gamesCount":5}
]}
```

### 3.2 Resultados — partidos de una liga ✅

```
GET /service-api/result/web/api/v3/games
    ?champId={id}&dateFrom={ts}&dateTo={ts}&lng=es&ref=156
```

⚠️ Versión **v3**, no v2. Las versiones no son uniformes entre endpoints.

Sin partidos en la ventana devuelve:

```json
{"count":0}
```

✅ **Cuando `count` es 0, la clave `items` no existe.** Acceder con `data["items"]`
lanza `KeyError`. Usar siempre `data.get("items", [])`.

Con datos:

```json
{"count":8,"items":[{
  "id":738767054,
  "sportId":1,
  "champId":2664249,
  "champName":"Australia. NPL Victoria",
  "opp1":"Green Gully",
  "opp2":"Dandenong City (Melbourne)",
  "opp1Ids":[14173],
  "opp2Ids":[48273],
  "score":"0:0 (0:0,0:0)",
  "dopInfo":"",
  "hasSubGame":true,
  "dateStart":1784885400,
  "subGame":[
    {"title":"Saques de esquina","score":"4:6 (3:3,1:3)"},
    {"title":"Resultados combinados","score":"Goles: 0 Saques de esquina: 10 Tarjetas: 0"}
  ],
  "matchInfos":{"2":"Green Gully Reserve (Melbourne)","9":"+11°C"},
  "stadiumId":6109,
  "champCountry":4
}]}
```

❌ **No existe consulta por partido individual.** Probadas y fallidas con 400:
`?gameId={id}` y `?id={id}`. Hay que consultar por `champId` y filtrar en cliente.

### 3.3 LineFeed — cuotas en bloque ✅

```
GET /service-api/LineFeed/Get1x2_VZip
    ?count={n}&lng=es&cfview=2&mode=4&country=97&partner=156&virtualSports={bool}
```

⚠️ `virtualSports=true` incluye deportes simulados. **Poner `false`** salvo que
se quieran deliberadamente.

Variante por deporte observada: `?sports={sportId}&count=1000&lng=es`

### 3.4 LineFeed — catálogo de deportes ✅

```
GET /service-api/LineFeed/GetSportsShortZip
    ?lng=es&country=97&partner=156&virtualSports=true&gr=687&groupChamps=true
```

### 3.5 Live feed — mercados y marcador en vivo ✅

```
GET /service-api/main-live-feed/v1/gameEvents
    ?cfView=2&countEvents=250&country=97&gameId={id}&gr=687&grMode=4
    &lng=es&marketType=1&ref=156&supportedSpecialType=1
```

Devuelve todos los mercados abiertos más el bloque `scores`:

```json
"scores":{
  "fullScore":"1-1",
  "periodScores":[{"period":1,"scoreOpp1":25,"scoreOpp2":23}],
  "currentPeriod":3,
  "periodScoresStr":"25-23,22-25,21-17",
  "statusLineStr":"Evento en curso"
}
```

⚠️ **`statusLineStr` es texto localizado.** Cambia con `lng`. **Nunca parsearlo
para decidir liquidación.**

### 3.6 Live feed — info del partido ✅

```
GET /service-api/main-live-feed/v1/gameInfo
    ?country=97&gameId={id}&gr=687&lng=es&ref=156
```

Contiene el bloque:

```json
"statisticInfo":{"gameId":"...","stageId":"...","stageType":2,"status":3}
```

❌ **Semántica de `status` NO verificada.** Solo se observó `status: 3` en un
partido en curso (~41 min jugados). Se desconoce qué valor toma al finalizar, y
si el endpoint sigue respondiendo una vez el partido sale del feed en vivo.
**No usar `status` como flag de liquidación mientras no se compruebe.**

---

## 4. Hechos verificados sobre los identificadores

### 4.1 `game_id` es estable entre sistemas ✅

Caso comprobado: partido Green Gully vs Dandenong City.

```
LineFeed  (gameInfo)      id = 738767054
Results   (v3/games)      id = 738767054
```

**Consecuencia:** el matching se hace por igualdad de enteros. No se necesita
fuzzy matching, ni normalización de nombres de equipo, ni `difflib`.

### 4.2 `liga.id` del LineFeed == `champId` de resultados ✅

```
gameInfo → "liga":{"id":2664249,"name":"Australia. NPL Victoria"}
v3/games?champId=2664249 → champName "Australia. NPL Victoria", 8 partidos
```

**Consecuencia:** no hace falta tabla de equivalencia entre ligas.

**Consecuencia de diseño:** al crear una apuesta hay que persistir **`game_id` y
`champ_id`**. Sin `champ_id` no se puede consultar el resultado después, porque
no existe consulta por partido individual (§3.2).

---

## 5. Filtrado obligatorio de eventos agregados

La respuesta de `v3/games` **mezcla partidos reales con eventos combinados y
agregados**. Si entran al liquidador, se paga con marcadores que no corresponden
a ningún partido.

Casos reales observados en `champId=2664249`:

| id | opp1 | dopInfo | Tipo |
|---|---|---|---|
| 738767054 | Green Gully | `""` | ✅ partido real |
| 737967386 | Locales | `"5 Partidos"` | ❌ agregado de liga |
| 738796443 | Preston Lions/Oakleigh Cannons | `""` | ❌ combinado (2 equipos) |
| 738769319 | FC Heidelberg/St Albans/Green Gully | `""` | ❌ combinado (3 equipos) |

⚠️ **Hacen falta los dos filtros.** Los combinados tienen `dopInfo` vacío, así
que filtrar solo por `dopInfo` los deja pasar.

```python
def es_partido_real(item: dict) -> bool:
    if len(item.get("opp1Ids", [])) != 1:
        return False
    if len(item.get("opp2Ids", [])) != 1:
        return False
    if item.get("dopInfo"):
        return False
    return True
```

---

## 6. Parser de marcador

✅ El formato de `score` **es inconsistente**. Formas observadas:

```
"1:1 (1:1,0:0)"      con espacio antes del paréntesis
"5:5(3:1,2:4)"       sin espacio
"2:2(1:0,1:2)"       sin espacio
"0:0 (0:0,0:0)"      con espacio
```

```python
import re

_RX_SCORE = re.compile(r"^(\d+):(\d+)\s*(?:\(([^)]*)\))?$")

def parse_score(raw: str) -> dict | None:
    """Devuelve None si el formato no se reconoce. Nunca asume 0:0."""
    if not raw:
        return None
    m = _RX_SCORE.match(raw.strip())
    if not m:
        return None
    return {
        "local": int(m.group(1)),
        "visitante": int(m.group(2)),
        "periodos_raw": m.group(3) or "",
    }
```

⚠️ `parse_score` devolviendo `None` **nunca debe tratarse como 0:0**. Es
`requiere_admin`.

⚠️ El campo `score` de `subGame` **no siempre es numérico**. Ejemplo real:
`"Resultados combinados"` → `"Goles: 2 Saques de esquina: 8 Tarjetas: 5"`.
Ese subgame necesita su propio parser o debe excluirse.

---

## 7. Esquema comprimido `_VZip`

Los endpoints `LineFeed/*Zip` usan claves de una o dos letras.

| Clave | Significado |
|---|---|
| `I` | game id |
| `CI` | constId |
| `N` | número interno |
| `S` | timestamp de inicio |
| `O1` / `O2` | nombre de los equipos |
| `O1I` / `O2I` | id de los equipos |
| `L` | nombre de la liga |
| `LI` | id de la liga (= `champId`, ver §4.2) |
| `SI` | sportId |
| `SS` / `SST` | estado (❌ semántica no verificada) |
| `E[]` | array de cuotas |

Dentro de cada elemento de `E[]`:

| Clave | Significado |
|---|---|
| `T` | tipo de mercado |
| `C` | cuota decimal |
| `CV` | cuota americana |
| `P` | parámetro (línea del total o hándicap) |
| `G` | groupId |
| `CE` | isCenter (línea principal) |

---

## 8. Tipos de mercado

### 8.1 Verificados por inspección ✅

| `T` | Mercado |
|---|---|
| 1 | 1 (local) |
| 2 | X (empate) |
| 3 | 2 (visitante) |
| 4 | 1X |
| 5 | 12 |
| 6 | X2 |
| 7 | Hándicap local (`P` = línea) |
| 8 | Hándicap visitante |
| 9 | Total más de (`P` = línea) |
| 10 | Total menos de |
| 11 | Total individual local, más de |
| 12 | Total individual local, menos de |
| 13 | Total individual visitante, más de |
| 14 | Total individual visitante, menos de |
| 180 | Ambos equipos marcan — Sí |
| 181 | Ambos equipos marcan — No |

### 8.2 No verificados ❌

Aparecen en las respuestas reales y **se desconoce su significado**: `T182`,
`T183`, `T191`, `T192`, `T196`, `T197`, `T206`, `T207`, `T211`, `T212`, `T731`,
`T732`, `T733`, `T768`, `T883`–`T887`, `T971`, `T972`, `T1737`, `T1738`,
`T1761`, `T1762`, `T1770`, `T1771`, `T1774`–`T1777`, `T1782`, `T1783`, `T1868`,
`T1869`, `T2235`, `T2236`, `T3827`–`T3830`, `T14159`–`T14176`, y otros.

La traducción está en los ficheros `bets_model_short_es_{NN}.json` y
`bets_model_map_short_es.json`, observados en el tráfico de red. **Su URL base no
se ha capturado.** Pendiente.

⚠️ **Regla de producto:** no habilitar ningún mercado cuyo `T` no esté en §8.1
y no tenga regla de liquidación escrita y probada en `REGLAS_LIQUIDACION.md`.

---

## 9. sportId ✅

| id | Deporte |
|---|---|
| 1 | Fútbol |
| 2 | Hockey sobre hielo |
| 3 | Baloncesto |
| 4 | Tenis |
| 5 | Béisbol |
| 6 | Voleibol |
| 10 | Tenis de mesa |
| 13 | Fútbol americano |

---

## 10. Ligas sintéticas — whitelist obligatoria

De 141 ligas de fútbol devueltas por `v2/champs`, más de la mitad son simuladas:

```
FC 26. Esports World Cup              167 partidos
Esoccer Battle Volta                  280
Short Football 2x2                    144
UEFA Conference League. Alternativos  799
Volta / Student League / Virtual eComp / Subsoccer / MLS+
```

Iterar todas las ligas procesa miles de eventos simulados.

⚠️ **Usar whitelist de `champId`, nunca blacklist por nombre.** Los nombres
cambian; los ids no.

---

## 11. Pregunta abierta: ¿publica `/result/` partidos en curso?

❌ **No resuelta.** La respuesta de `v3/games` **no tiene ningún campo de
estado**: ni `finished`, ni `status`, ni `isLive`.

Evidencia indirecta a favor de que solo publica finalizados: consultando la
**misma ventana exacta** (`1784792100`–`1784878500`) en dos momentos distintos,
el número de ligas pasó de 138 a 141. La ventana ya estaba cerrada, así que los
partidos se indexan por `dateStart` pero **se publican al terminar**.

No es concluyente.

### Estrategia recomendada: no depender de la respuesta

En vez de resolver la pregunta, diseñar para que no importe:

```
Consultar el mismo game_id dos veces, separadas ~15 min.

score idéntico en ambas  Y  (ahora - dateStart) >= 2h
    → confirmado

score distinto
    → sigue en curso, reintentar

sin resultado / parse_score None / evento combinado
    → requiere_admin
```

Un marcador estable durante 15 minutos, con el partido pasado de su duración
normal, es final. Funciona publique o no publique en vivo, y no depende de un
campo que el proveedor puede cambiar sin avisar.

**Coste:** una consulta extra por liquidación. Aceptable frente al coste de
pagar mal.

---

## 12. Mapeo a los tres estados de `cascada_fuentes.py`

Según la regla 3 de `CLAUDE.md`, `cascada_fuentes.py` es el único módulo que
determina el estado de un resultado.

| Estado | Condición |
|---|---|
| `confirmado` | 1x devuelve el `game_id`, pasa `es_partido_real`, `parse_score` OK, y score estable según §11 |
| `no_confirmado` | 1x no devuelve el `game_id`, o `parse_score` devuelve `None` |
| `requiere_admin` | 1x y la fuente de respaldo discrepan, o el item es combinado/agregado |

**Nota de arquitectura:** 1x es a la vez fuente de líneas y de resultados. Eso
es un punto único de fallo. Debe mantenerse una segunda fuente en
`fuente_resultados/fallback/` — si no, `requiere_admin` solo se dispara por
errores de formato y nunca por contradicción de datos, que es el fallo caro.

---

## 13. Resumen de lo que falta verificar

| # | Pregunta | Bloquea |
|---|---|---|
| 1 | Valor de `statisticInfo.status` al finalizar un partido | Flag directo de finalización (§3.6) |
| 2 | Ventana máxima de `dateFrom`/`dateTo` | Estrategia de paginación (§2) |
| 3 | URL base de `bets_model_*.json` | Catálogo completo de mercados (§8.2) |
| 4 | Semántica de `SS` / `SST` en `_VZip` | Filtrado de estado en el feed (§7) |
| 5 | Si el espejo `1x-bet.mobi` exige `x-hd` | Uso como respaldo (§1) |

Ninguna bloquea la Fase 1 (prepartido, mercados de §8.1, apuesta simple).
La #1 y la #3 se vuelven necesarias para live y para ampliar mercados.
