# ESQUEMA_DB.md — Modelo de datos, Fase 1

Contrato de persistencia para `bot/db/`.

**Alcance:** Fase 1. Prepartido, fútbol, CUP y USD, apuesta simple y combinada.

---

## 1. Principios no negociables

### 1.1 El dinero es entero, nunca decimal ni flotante

SQLite no tiene tipo `Decimal`. Guardar dinero en `REAL` produce errores que se
acumulan y solo se descubren al conciliar.

```
Almacenamiento:  INTEGER, en centavos
En Python:       Decimal, siempre
Conversión:      en el borde, en una sola función
```

```python
CENT = Decimal("100")

def a_centavos(monto: Decimal) -> int:
    return int((monto * CENT).to_integral_value(ROUND_HALF_UP))

def a_decimal(centavos: int) -> Decimal:
    return Decimal(centavos) / CENT
```

Las cuotas siguen el mismo criterio, en **milésimas**: `1.788` → `1788`. Es
exacto para los 3 decimales que devuelve la API.

⚠️ **Ningún `float` en ninguna parte del sistema de dinero.**

### 1.2 El ledger es la verdad; el saldo es caché

`movimientos` es un libro de asientos, solo se inserta, nunca se edita ni se
borra. `saldos` es una materialización que se actualiza **en la misma
transacción**.

Debe cumplirse siempre:

```sql
SELECT usuario_id, moneda, SUM(centavos) FROM movimientos GROUP BY 1,2
=
SELECT usuario_id, moneda, centavos FROM saldos
```

Si difieren, hay un bug y hay que parar. Sin ledger, un error de liquidación es
irrastreable.

### 1.3 La cuota se congela al aceptar

`cuota_milesimas` se copia del feed en el momento de aceptar y **no se vuelve a
consultar nunca**. Liquidar con la cuota actual en vez de la pactada es cambiar
el contrato después del hecho.

### 1.4 Resolver y pagar son dos pasos distintos

Un ticket puede estar `ganado` y todavía no pagado. El pago es un `movimiento`
separado. Así el fallo entre ambos es detectable y reparable.

### 1.5 El pago es idempotente

Un ticket **no puede pagarse dos veces**, ni aunque el liquidador corra dos
veces. Se garantiza con índice único en base de datos, no con un `if` en el
código (§4).

---

## 2. DDL

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------
CREATE TABLE usuarios (
    id                   INTEGER PRIMARY KEY,   -- telegram user id
    username             TEXT,
    creado_ts            INTEGER NOT NULL,
    bloqueado_hasta_ts   INTEGER,               -- autoexclusión; NULL = activo
    deposito_diario_max  INTEGER                -- centavos; NULL = por defecto
);

-- ---------------------------------------------------------------
-- Un saldo por usuario y moneda. Cada moneda es un libro aparte.
CREATE TABLE saldos (
    usuario_id  INTEGER NOT NULL REFERENCES usuarios(id),
    moneda      TEXT    NOT NULL,
    centavos    INTEGER NOT NULL DEFAULT 0 CHECK (centavos >= 0),
    PRIMARY KEY (usuario_id, moneda)
);

-- ---------------------------------------------------------------
-- Una apuesta simple es un ticket con UNA selección.
-- Una combinada es un ticket con N. Mismo modelo, misma liquidación.
CREATE TABLE tickets (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id        INTEGER NOT NULL REFERENCES usuarios(id),
    moneda            TEXT    NOT NULL,
    stake_cent        INTEGER NOT NULL CHECK (stake_cent > 0),
    cuota_milesimas   INTEGER NOT NULL CHECK (cuota_milesimas >= 1000),
    payout_pot_cent   INTEGER NOT NULL CHECK (payout_pot_cent > 0),
    estado            TEXT    NOT NULL DEFAULT 'pendiente',
    creado_ts         INTEGER NOT NULL,
    resuelto_ts       INTEGER,
    CHECK (estado IN ('pendiente','ganado','perdido','nulo','requiere_admin'))
);

-- ---------------------------------------------------------------
CREATE TABLE selecciones (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id             INTEGER NOT NULL REFERENCES tickets(id),

    -- Identificadores 1x. champ_id es OBLIGATORIO: no existe consulta
    -- por partido individual (ESPECIFICACION_FUENTE §3.2).
    game_id               INTEGER NOT NULL,
    champ_id              INTEGER NOT NULL,
    sport_id              INTEGER NOT NULL,

    -- Mercado
    market_type           INTEGER NOT NULL,     -- el T
    parametro_centesimas  INTEGER,              -- 2.5 -> 250; NULL si no aplica
    cuota_milesimas       INTEGER NOT NULL CHECK (cuota_milesimas >= 1000),

    -- Snapshot descriptivo, congelado al aceptar
    equipo_local          TEXT    NOT NULL,
    equipo_visitante      TEXT    NOT NULL,
    inicio_ts             INTEGER NOT NULL,

    -- Resolución
    estado                TEXT    NOT NULL DEFAULT 'pendiente',
    marcador_raw          TEXT,                 -- el score tal cual llegó
    resuelto_ts           INTEGER,

    CHECK (estado IN ('pendiente','ganada','perdida','nula','requiere_admin'))
);

-- ---------------------------------------------------------------
-- Libro de asientos. Solo INSERT. Nunca UPDATE ni DELETE.
CREATE TABLE movimientos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id  INTEGER NOT NULL REFERENCES usuarios(id),
    moneda      TEXT    NOT NULL,
    centavos    INTEGER NOT NULL,          -- con signo: negativo = sale
    tipo        TEXT    NOT NULL,
    ticket_id   INTEGER REFERENCES tickets(id),
    ts          INTEGER NOT NULL,
    nota        TEXT,
    CHECK (tipo IN ('deposito','retiro','stake',
                    'payout','devolucion','ajuste_admin'))
);

-- ---------------------------------------------------------------
-- IDEMPOTENCIA DE PAGO.
-- Un ticket no puede tener dos payout ni dos devoluciones.
-- La garantía es la base de datos, no el código.
CREATE UNIQUE INDEX ux_mov_pago_unico
    ON movimientos(ticket_id, tipo)
    WHERE tipo IN ('payout','devolucion');

-- ---------------------------------------------------------------
-- Consulta caliente: exposición por evento (LIMITES §4).
CREATE INDEX ix_sel_game_estado  ON selecciones(game_id, estado);
CREATE INDEX ix_sel_ticket       ON selecciones(ticket_id);
CREATE INDEX ix_sel_champ_estado ON selecciones(champ_id, estado);
CREATE INDEX ix_tickets_estado   ON tickets(estado);
CREATE INDEX ix_mov_usuario      ON movimientos(usuario_id, moneda, ts);
```

---

## 2.1 Observaciones de resultado (regla de estabilidad)

`fuente_resultados/cascada_fuentes.py` decide `confirmado` / `no_confirmado` /
`requiere_admin` según si el marcador se mantuvo estable un tiempo mínimo
(ESPECIFICACION_FUENTE §11). Esa regla necesita persistencia: un reinicio del
bot no puede borrar lo observado, o se volvería a contar la estabilidad desde
cero en cada arranque.

```sql
-- ---------------------------------------------------------------
CREATE TABLE observaciones_resultado (
    game_id                  INTEGER PRIMARY KEY,
    marcador_raw             TEXT    NOT NULL,
    visto_primera_vez_ts     INTEGER NOT NULL,
    ultima_consulta_ts       INTEGER NOT NULL
);
```

Semántica (migración `002_observaciones_resultado.sql`):

- Marcador igual al guardado → solo actualiza `ultima_consulta_ts`.
- Marcador distinto → reemplaza y **resetea** `visto_primera_vez_ts`.

`visto_primera_vez_ts` es "desde cuándo el marcador no cambia". Es lo que
`cascada_fuentes.py` compara contra los 15 minutos de estabilidad exigidos.

---

## 3. Por qué cada índice

| Índice | Consulta que sirve |
|---|---|
| `ix_sel_game_estado` | Exposición por evento. Corre en **cada aceptación de apuesta**, dentro de la transacción. Sin este índice, escaneo completo con el saldo bloqueado |
| `ix_sel_champ_estado` | Agrupar apuestas pendientes por liga para consultar resultados |
| `ix_tickets_estado` | Barrido del liquidador |
| `ix_mov_usuario` | Stake diario del usuario (`LIMITES` §8) |

---

## 4. Reglas de escritura

| Tabla | Quién escribe |
|---|---|
| `tickets`, `selecciones` | Solo `core/apuestas.py`, al aceptar |
| `movimientos` tipo `stake` | Solo `core/apuestas.py`, misma transacción |
| `movimientos` tipo `payout` / `devolucion` | **Solo `settlement_engine.py`** (`CLAUDE.md` regla 4) |
| `movimientos` tipo `ajuste_admin` | Solo flujo de admin, con `nota` obligatoria |
| `selecciones.estado` | Solo `settlement_engine.py` |
| `saldos` | Nunca directo: siempre junto al `movimiento` que lo causa |
| `observaciones_resultado` | Solo `fuente_resultados/cascada_fuentes.py` |

### Transacción de aceptación

```
BEGIN IMMEDIATE
  1. SELECT saldo del usuario (bloquea la fila)
  2. Verificar saldo suficiente
  3. INSERT ticket + selecciones
  4. Recalcular exposición del evento INCLUYENDO este ticket
  5. Verificar todos los límites de LIMITES.md
  6. Si algo falla → ROLLBACK
  7. INSERT movimiento tipo 'stake' (negativo)
  8. UPDATE saldos
COMMIT
```

⚠️ `BEGIN IMMEDIATE`, no el diferido por defecto. Con el diferido, dos apuestas
simultáneas pasan las dos comprobaciones y el límite se salta.

### Transacción de pago

```
BEGIN IMMEDIATE
  1. Verificar ticket.estado == 'ganado' o 'nulo'
  2. INSERT movimiento 'payout' o 'devolucion'
     → si el índice único falla, YA estaba pagado: ROLLBACK sin error
  3. UPDATE saldos
COMMIT
```

El fallo del índice único **no es un error**: es la idempotencia funcionando.

---

## 5. Consulta de exposición

```sql
-- Peor caso de un evento. Se ejecuta dentro de la transacción de aceptación.
SELECT t.id, t.payout_pot_cent, s.market_type, s.parametro_centesimas
FROM selecciones s
JOIN tickets t ON t.id = s.ticket_id
WHERE s.game_id = :game_id
  AND s.estado  = 'pendiente'
  AND t.estado  = 'pendiente'
  AND t.moneda  = :moneda;
```

⚠️ Devuelve **el payout del ticket completo**, no una parte proporcional. Si una
combinada de 4 patas toca este partido y gana, se paga entera. Prorratear
subestima la exposición.

---

## 6. Fuera de alcance en Fase 1

No se modela todavía. Añadirlo antes de necesitarlo genera esquema muerto:

- Cripto y su banca (Fase 2)
- Cashback VIP y comisiones de referido
- Mercados en vivo y suspensión
- Caché local del feed de partidos
- Bet Builder / mercados correlacionados

---

## 7. Migraciones

Esquema versionado con `PRAGMA user_version`. Cada cambio es un archivo
numerado, y **solo hacia adelante**: no se editan migraciones ya aplicadas.

```
bot/db/migraciones/
  001_esquema_inicial.sql
  002_observaciones_resultado.sql
```

Motivo: la base de producción tiene dinero real. Reescribir una migración ya
aplicada deja el entorno de desarrollo y el de producción en estados distintos,
y eso no se nota hasta que algo cuadra mal.
