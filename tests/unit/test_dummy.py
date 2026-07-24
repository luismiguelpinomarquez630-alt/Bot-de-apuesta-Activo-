def test_dummy_pipeline_ok():
    assert 1 + 1 == 2


def test_dummy_db_fixture(db_conn):
    cur = db_conn.execute("SELECT 1")
    assert cur.fetchone() == (1,)
