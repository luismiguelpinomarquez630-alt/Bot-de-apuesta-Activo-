# CACHE_CUOTAS.md — Snapshot de cuotas para aceptación

Contrato del componente que media entre el feed de 1x y la aceptación de
apuestas. Prerrequisito de `bot/core/apuestas.py`.

**Alcance:** Fase 1, prepartido.

---

## 1. El problema que resuelve

Entre que el usuario ve una cuota y confirma la apuesta pasan segundos. En ese
intervalo la cuota pudo cambiar en 1x. Si se acepta a la cuota que el usuario vio
y ya no es válida, el bot opera a un precio desactualizado — la versión prepartido
del riesgo de latencia de live.

También el inverso: consultar el feed entero cada vez que alguien confirma una
apuesta es lento y golpea la API sin necesidad, porque las cuotas prepartido no
se mueven segundo a segundo.

La caché es el punto medio: **una consulta al feed sirve a muchas aceptaciones
durante una ventana corta**, y la aceptación valida contra ese snapshot, no
contra lo que el usuario vio en pantalla.

---

## 2. Qué NO es

⚠️ Esto no es caché de rendimiento. Es un **snapshot con caducidad para
control de precio**. La diferencia importa:

- Una caché de rendimiento sirve datos viejos si los nuevos no están listos.
- Este snapshot, si está vencido, **bloquea la aceptación**. Nunca acepta contra
  datos que sabe caducados.

Servir una cuota vencida es exactamente el fallo que este componente existe para
evitar.

---

## 3. Ubicación en capas

`bot/fuente_resultados/cache_cuotas.py` — CAPA 1.

Llama a `cliente_1x.obtener_cuotas` (misma capa) y expone lectura a `core/`
(CAPA 2). No importa de `core`. Respeta la dirección CAPA3→CAPA2→CAPA1.

⚠️ Igual que el resto de `fuente_resultados/`, **no decide nada de negocio**: no
valida límites, no acepta apuestas, no toca saldos. Solo entrega la cuota vigente
de un mercado, o indica que no la tiene fresca.

---

## 4. Estado en memoria, no en base de datos

El snapshot vive en memoria del proceso, no en SQLite.

Razones:
- Es efímero por diseño (TTL de segundos). Persistirlo no aporta.
- Tras un reinicio, la primera consulta lo repuebla. Un snapshot viejo
  recuperado de disco sería justo lo que no queremos servir.
- Evita escrituras constantes a la base que compiten con la transacción de
  aceptación.

**Consecuencia:** si hay más de un proceso (no es el caso en Fase 1, pero
conviene anotarlo), cada uno tiene su propio snapshot. No se comparte. En Fase 1
hay un solo proceso, así que no aplica.

---

## 5. Estructura

```python
@dataclass(frozen=True)
class CuotaVigente:
    game_id: int
    champ_id: int
    market_type: int
    parametro: Decimal | None
    cuota_milesimas: int
    capturada_ts: int          # cuándo se trajo del feed


@dataclass
class _Snapshot:
    por_clave: dict[ClaveMercado, CuotaVigente]
    sport_id: int
    refrescado_ts: int
```

La clave de un mercado es la tupla que lo identifica sin ambigüedad:

```python
ClaveMercado = tuple[int, int, Decimal | None]
#                    game_id, market_type, parametro
```

⚠️ El `parametro` es parte de la clave. `total más de 2.5` y `total más de 3.5`
son el mismo `game_id` y el mismo `market_type` (9), pero mercados distintos.
Sin el parámetro en la clave, uno pisa al otro.

---

## 6. TTL y frescura

```python
TTL_S = 90                 # revisado: ver nota de ESPECIFICACION_FUENTE §0
```

Una `CuotaVigente` se considera **fresca** si:

```python
ahora_ts - cuota.capturada_ts <= TTL_S
```

⚠️ La frescura se mide sobre `capturada_ts` de la cuota, **no** sobre
`refrescado_ts` del snapshot. Si un refresco falla y se conserva el snapshot
anterior (§7), las cuotas siguen envejeciendo desde su captura real. No se
"rejuvenecen" por el intento de refresco.

⚠️ **Revisado de 30s a 90s.** `Get1x2_VZip` contra la fuente real de
producción (`provider.betfantasy.bet`, ESPECIFICACION_FUENTE §0) tarda
4-10s y pesa 100+KB — bastante más que 1x. El TTL tiene que quedar
cómodamente por encima del intervalo de refresco (§7, ahora 45s) para que
un ciclo lento no deje el snapshot vencido antes del siguiente refresco.

---

## 7. Refresco

```python
async def refrescar(sport_id: int, count: int, ahora_ts: int) -> None:
    """Trae el feed y reemplaza el snapshot de ese deporte.
    Si obtener_cuotas falla, el snapshot anterior se conserva:
    las cuotas seguirán envejeciendo y venciendo por su cuenta (§6).
    No se sirve nada como si fuera fresco."""
```

Quién lo dispara: un job de APScheduler cada 45s (`INTERVALO_REFRESCO_S`,
menos que el TTL de 90s, para que casi siempre haya snapshot fresco). Esto
vive en la capa de orquestación (`__main__` / scheduler), no dentro de este
módulo.

⚠️ **Revisado de 15s a 45s.** Con `Get1x2_VZip` tardando 4-10s contra la
fuente real (§6), refrescar cada 15s solapaba corridas. 45s deja margen de
sobra sin acercarse al TTL.

⚠️ **`AsyncIOScheduler`, no `BackgroundScheduler`.** El cliente httpx está atado
al event loop; un scheduler en hilos con su propio loop rompe (ya anotado en
`CONTEXTO_SESION.md` sobre el singleton de `cliente_1x`).

Si un refresco falla:
- No se borra el snapshot anterior.
- Se registra el fallo (observabilidad).
- Las cuotas vencen solas. Si el feed sigue caído más que el TTL, todas las
  cuotas quedan vencidas y la aceptación se bloquea (§8). Correcto: sin feed
  fresco, no se aceptan apuestas.

---

## 8. Lectura para aceptación

La única función que `core/apuestas.py` usa al aceptar:

```python
def obtener_cuota_fresca(
    game_id: int,
    market_type: int,
    parametro: Decimal | None,
    ahora_ts: int,
) -> CuotaVigente | None:
    """Devuelve la cuota SOLO si existe y está fresca (§6).
    None si no está en el snapshot o venció.
    None SIEMPRE bloquea la aceptación: nunca se acepta a ciegas."""
```

Contrato con el llamador:

| Resultado | Qué hace `apuestas.py` |
|---|---|
| `CuotaVigente` fresca | Sigue la validación de precio (§9) |
| `None` | **Rechaza la apuesta.** "Cuota no disponible, intentá de nuevo" |

⚠️ `None` nunca es "aceptá igual con lo que tengas". Es "no hay precio válido
ahora mismo". Es la línea que impide operar a ciegas.

---

## 9. Validación de precio en la aceptación

Esto vive en `apuestas.py`, no acá, pero se especifica porque es el motivo de
existir de la caché.

El usuario vio una cuota (`cuota_vista_milesimas`) cuando armó la apuesta. Al
confirmar:

Política decidida (Fase 1):

```
UMBRAL_BAJA = Decimal("0.02")   # 2%

vigente = obtener_cuota_fresca(...)

si vigente is None:
    → rechazar: "cuota no disponible, intentá de nuevo"

si vigente.cuota_milesimas >= cuota_vista:
    → la cuota es igual o MEJORÓ para el usuario.
      Sellar a cuota_vista (la que vio).
      La mejora se la queda la banca.

si vigente.cuota_milesimas < cuota_vista:
    caida = (cuota_vista - vigente.cuota_milesimas) / cuota_vista
    si caida <= UMBRAL_BAJA:
        → bajó poco (≤2%). Sellar a vigente.cuota_milesimas
          (la nueva, peor). Sin molestar al usuario.
    si caida > UMBRAL_BAJA:
        → bajó bastante (>2%). NO aceptar en silencio.
          Mostrar la nueva y pedir reconfirmación explícita.
          Si reconfirma → sellar a vigente. Si no → cancelar.
```

Las tres reglas duras detrás de esto:

⚠️ **Nunca se sella una cuota mejor que la vigente de 1x.** Cuando la cuota
mejora, se sella a la vista (menor), no a la nueva. Sellar por encima de lo que
1x ofrece ahora regala valor. La cuota sellada es siempre `<=` la vigente.

⚠️ **Una baja pequeña (≤2%) se acepta a la cuota nueva, no a la vista.** El
usuario se lleva la peor de las dos, pero la diferencia es mínima y se evita la
fricción de reconfirmar por centésimas. En prepartido casi todos los cambios
caen aquí.

⚠️ **Una baja grande (>2%) nunca se acepta en silencio.** Se reconfirma. Es la
línea que impide que el usuario descubra en su ticket una cuota mucho peor de la
que tocó.

---

## 10. Pendiente de decisión

| # | Decisión | Afecta |
|---|---|---|
| 1 | Si la cuota mejora, ¿se sella a la nueva o a la vista? | §9. Sellar a la vista es más simple y nunca perjudica al bot |
| 2 | ¿Una tolerancia de cambio que reconfirme solo si el movimiento supera un umbral, o reconfirmar ante cualquier baja? | §9, UX |

TTL (§6, 90s) e intervalo de refresco (§7, 45s) ya no están pendientes:
revisados contra los tiempos reales de `provider.betfantasy.bet`
(ESPECIFICACION_FUENTE §0).

Recomendación para #1: **sellar a la cuota vista** cuando mejora. Nunca
perjudica al bot (sella más barato de lo que podría), y evita explicarle al
usuario por qué cobró una cuota distinta de la que tocó. La mejora se la queda
la banca, que es lo prudente en Fase 1.

---

## 11. Tests

Sin red. `cliente_1x.obtener_cuotas` mockeado.

- Refresco puebla el snapshot; `obtener_cuota_fresca` devuelve la cuota
- Cuota dentro del TTL → fresca; pasado el TTL → `None`
- Mercado que no está en el snapshot → `None`
- `parametro` distinto → clave distinta: total 2.5 y total 3.5 no se pisan
- Refresco que falla conserva el snapshot anterior, y sus cuotas siguen
  venciendo por `capturada_ts`, no se rejuvenecen
- Feed caído más que el TTL → todas las cuotas vencen → todo `None`
