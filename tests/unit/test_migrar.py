import sqlite3

from bot.db.migrar import aplicar_migraciones


def test_aplica_migraciones_sobre_db_vacia():
    conn = sqlite3.connect(":memory:")

    aplicar_migraciones(conn)

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 2  # 001_esquema_inicial + 002_observaciones_resultado

    tablas = {fila[0] for fila in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"usuarios", "saldos", "tickets", "selecciones", "movimientos", "observaciones_resultado"} <= tablas
    conn.close()


def test_aplicar_dos_veces_es_idempotente():
    conn = sqlite3.connect(":memory:")

    aplicar_migraciones(conn)
    aplicar_migraciones(conn)  # no debe re-ejecutar y romper con "table already exists"

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 2
    conn.close()


def test_solo_aplica_las_migraciones_pendientes():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA user_version = 1")  # simula que 001 ya se aplicó en otra corrida
    conn.execute(
        "CREATE TABLE usuarios (id INTEGER PRIMARY KEY, username TEXT, creado_ts INTEGER NOT NULL, "
        "bloqueado_hasta_ts INTEGER, deposito_diario_max INTEGER)"
    )

    aplicar_migraciones(conn)  # solo debería correr 002

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 2
    tablas = {fila[0] for fila in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "observaciones_resultado" in tablas
    conn.close()
