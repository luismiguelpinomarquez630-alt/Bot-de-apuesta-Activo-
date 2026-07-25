"""Aplica migraciones pendientes contra `bot/db/migraciones/`.

Versionado con `PRAGMA user_version` (ESQUEMA_DB.md §7): cada archivo
numerado que se aplica actualiza `user_version` a su número. Solo hacia
adelante — un archivo cuyo número ya quedó por debajo de `user_version` se
salta, nunca se re-ejecuta.
"""

import sqlite3
from pathlib import Path

MIGRACIONES_DIR = Path(__file__).parent / "migraciones"


def aplicar_migraciones(conn: sqlite3.Connection) -> None:
    version_actual = conn.execute("PRAGMA user_version").fetchone()[0]
    for archivo in sorted(MIGRACIONES_DIR.glob("*.sql")):
        numero = int(archivo.name.split("_", 1)[0])
        if numero <= version_actual:
            continue
        conn.executescript(archivo.read_text())
        # PRAGMA no admite parámetros ligados ("?"): numero sale del nombre
        # de archivo, no de una entrada externa.
        conn.execute(f"PRAGMA user_version = {numero}")
    conn.commit()
