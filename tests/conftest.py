import os
import tempfile

import pytest

# Point the app at an isolated throw-away database before it is imported.
_TMP = tempfile.mkdtemp(prefix="suscept-test-")
os.environ["APP_DB_PATH"] = os.path.join(_TMP, "test.db")


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app, get_db
    from app import db as db_mod

    # Fresh schema for every test.
    path = os.environ["APP_DB_PATH"]
    if os.path.exists(path):
        os.remove(path)
    for suffix in ("-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.remove(p)
    db_mod.init_db(path)

    with TestClient(app) as c:
        yield c


def make_csv(rows: list[list[str]]) -> bytes:
    header = "记录号,修订号,匿名患者号,UTC采样日,菌种,药物,MIC\n"
    body = "\n".join(",".join(r) for r in rows)
    return (header + body + "\n").encode("utf-8")
