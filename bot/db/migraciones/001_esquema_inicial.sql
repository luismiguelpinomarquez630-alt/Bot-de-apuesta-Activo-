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
