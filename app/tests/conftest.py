"""Test setup: a throwaway config folder and the SQL Server test database ClearScreen_Test
(local default instance, Windows login). The embedding service and Groq are replaced by fakes."""
import hashlib
import os
import tempfile

TEST_DATA = tempfile.mkdtemp(prefix="cls_test_")
os.environ["CLS_DATA_DIR"] = TEST_DATA            # before any clearscreen import

import pytest                                     # noqa: E402

TEST_SQL = os.environ.get("CLS_TEST_SQL", "Driver={ODBC Driver 18 for SQL Server};Server=localhost;"
                          "Database=ClearScreen_Test;Trusted_Connection=yes;Encrypt=yes;TrustServerCertificate=yes")


def fake_embed(texts):
    """Bag of words hashed into 64 dims: identical text -> 100, unrelated text -> low."""
    out = []
    for t in texts:
        v = [0.0] * 64
        for w in t.lower().split():
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1
        out.append(v)
    return out


@pytest.fixture
def sql():
    """An empty ClearScreen schema in the test database."""
    from clearscreen import db, similarity
    con = db.connect(TEST_SQL)
    db.create_schema(con)
    cur = con.cursor()
    for t in ("WL_Payload_Chunks", "WL_Chunk_Vectors", "WL_Records"):
        cur.execute(f"DELETE FROM dbo.{t}")
    con.commit()
    similarity._INDEX.clear()
    yield con
    con.close()
