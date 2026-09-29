"""PostgreSQL + pgvector swap (P0): parity against the shipped SQLite/numpy
seams. Runs whenever REGCOMPASS_PG_DSN points at a database with the pgvector
extension available (see docs/POSTGRES.md); skips otherwise - CI carries no
postgres, the swap is verified on demand."""

from __future__ import annotations

import os

import numpy as np
import pytest

DSN = os.environ.get("REGCOMPASS_PG_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="REGCOMPASS_PG_DSN not set (postgres parity suite is opt-in)"
)

DIM = 32


@pytest.fixture()
def store():
    from regcompass.pg import PgEmbeddingStore

    s = PgEmbeddingStore(DSN, dim=DIM)
    s._conn.run("DELETE FROM chunk_embeddings")
    yield s
    s._conn.run("DROP TABLE IF EXISTS chunk_embeddings")
    s.close()


def _vectors(n: int, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal((n, DIM)).astype(np.float32)


def test_store_and_load_round_trip(store):
    vecs = _vectors(5)
    for i, v in enumerate(vecs):
        store.store_embedding(f"doc:c{i:04d}", v)
    ids, matrix = store.load_embeddings()
    assert ids == [f"doc:c{i:04d}" for i in range(5)]
    np.testing.assert_allclose(matrix, vecs, rtol=1e-5)


def test_store_is_replace_not_duplicate(store):
    v1, v2 = _vectors(2)
    store.store_embedding("doc:c0001", v1)
    store.store_embedding("doc:c0001", v2)
    ids, matrix = store.load_embeddings()
    assert ids == ["doc:c0001"]
    np.testing.assert_allclose(matrix[0], v2, rtol=1e-5)


def test_dimension_mismatch_refused(store):
    with pytest.raises(ValueError, match="dim"):
        store.store_embedding("doc:c0001", np.ones(DIM + 1, dtype=np.float32))


def test_search_parity_with_numpy_index(store):
    """The credibility bar: pgvector's <=> ordering and scores must agree
    with the shipped exact numpy index over the same vectors."""
    from regcompass.pg import PgVectorIndex
    from regcompass.storage import VectorIndex

    vecs = _vectors(50)
    ids = [f"doc:c{i:04d}" for i in range(50)]
    for cid, v in zip(ids, vecs):
        store.store_embedding(cid, v)

    query = _vectors(1, seed=99)[0]
    numpy_top = VectorIndex(ids, vecs).search(query, top_k=10)
    pg_top = PgVectorIndex(store).search(query, top_k=10)

    assert [cid for cid, _ in pg_top] == [cid for cid, _ in numpy_top]
    for (_, a), (_, b) in zip(pg_top, numpy_top):
        assert abs(a - b) < 1e-5


def test_migrate_from_sqlite(store, tmp_path):
    from regcompass.pg import PgVectorIndex, migrate_embeddings
    from regcompass.storage import Storage

    sqlite = Storage(tmp_path / "db.sqlite")
    sqlite.apply_schema()
    sqlite.upsert_document("doc_001", "SG", "a" * 64)
    vecs = _vectors(4)
    for i, v in enumerate(vecs):
        cid = f"doc_001:c{i:04d}"
        sqlite.conn.execute(
            "INSERT INTO chunks (chunk_id, document_id, char_start, char_end,"
            " section_label, created_at) VALUES (?, ?, 0, 10, 's. 1', '')",
            (cid, "doc_001"),
        )
        sqlite.store_embedding(cid, v)

    n = migrate_embeddings(sqlite, DSN)
    assert n == 4
    top = PgVectorIndex(store).search(vecs[2], top_k=1)
    assert top[0][0] == "doc_001:c0002"
    assert top[0][1] > 0.999
