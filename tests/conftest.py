import sqlite3

import pytest


@pytest.fixture
def db_conn():
    conn = sqlite3.connect(":memory:")
    try:
        yield conn
    finally:
        conn.close()
