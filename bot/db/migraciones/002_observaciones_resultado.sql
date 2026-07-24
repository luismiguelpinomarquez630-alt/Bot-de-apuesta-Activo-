-- ---------------------------------------------------------------
-- Persistencia de la regla de estabilidad de marcador (ESPECIFICACION_FUENTE
-- §11). Un reinicio del bot no puede borrar lo observado, o cascada_fuentes.py
-- volvería a contar 15 minutos de estabilidad desde cero en cada arranque.
--
-- Semántica (aplicada por cascada_fuentes.py, único escritor de esta tabla):
--   marcador igual al guardado   -> solo actualiza ultima_consulta_ts
--   marcador distinto al guardado -> reemplaza y RESETEA visto_primera_vez_ts
--
-- visto_primera_vez_ts es "desde cuándo el marcador no cambia".
CREATE TABLE observaciones_resultado (
    game_id                  INTEGER PRIMARY KEY,
    marcador_raw             TEXT    NOT NULL,
    visto_primera_vez_ts     INTEGER NOT NULL,
    ultima_consulta_ts       INTEGER NOT NULL
);
