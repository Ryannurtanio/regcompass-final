"""PostgreSQL + pgvector implementation of the embedding seams (P0).

SQLite + numpy brute-force cosine is the shipped default (at 10^3-10^4
vectors a full scan is one matmul; an ANN index buys nothing).
This module is the documented PRODUCTION swap as REAL code behind the same
two seams storage.py defines:

- the embedding store (Storage.store_embedding / load_embeddings)
- VectorIndex (search(query, top_k) -> [(chunk_id, cosine)])

plus a one-call migrator from a working SQLite database. The DDL variant
lives in docs/schema_postgres.sql; docs/POSTGRES.md is the operator note
(including the llama-cpp-python + GBNF no-Ollama fallback engine).

Driver: pg8000 (pure Python, BSD licence - the license floor here is
Apache-2.0-compatible, which rules out the LGPL psycopg family). Install via
the optional extra: `uv sync --extra postgres`. Vectors travel as pgvector
text literals ('[0.1,0.2,...]'), so no driver-side type plugin is needed.

Everything here is exercised by tests/test_pg.py, which runs whenever
REGCOMPASS_PG_DSN is set (e.g. postgresql://localhost/regcompass_test) and
skips otherwise - CI has no postgres; the swap is verified on demand.
"""

from __future__ import annotations

import numpy as np

from .storage import Storage


def _vec_literal(vector: np.ndarray) -> str:
    v = np.asarray(vector, dtype=np.float32).ravel()
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


def _parse_dsn(dsn: str) -> dict:
    """postgresql://[user[:password]@][host][:port][/database] -> pg8000 kwargs."""
    from urllib.parse import urlparse

    u = urlparse(dsn)
    if u.scheme not in ("postgresql", "postgres"):
        raise ValueError(f"not a postgresql DSN: {dsn!r}")
    import getpass

    # pg8000 requires user explicitly; default to the OS user like psql does
    kwargs: dict = {"user": u.username or getpass.getuser()}
    if u.password:
        kwargs["password"] = u.password
    if u.hostname:
        kwargs["host"] = u.hostname
    if u.port:
        kwargs["port"] = u.port
    if u.path and u.path != "/":
        kwargs["database"] = u.path.lstrip("/")
    return kwargs


def connect(dsn: str):
    """A pg8000 native connection with autocommit off (callers commit)."""
    import pg8000.native

    return pg8000.native.Connection(**_parse_dsn(dsn))


class PgEmbeddingStore:
    """The Storage embedding seam on PostgreSQL + pgvector.

    Mirrors Storage.store_embedding / load_embeddings semantics: one vector
    per chunk_id, float32, replace on conflict."""

    def __init__(self, dsn: str, dim: int):
        self.dim = dim
        self._conn = connect(dsn)
        self._conn.run("CREATE EXTENSION IF NOT EXISTS vector")
        self._conn.run(
            "CREATE TABLE IF NOT EXISTS chunk_embeddings ("
            " chunk_id TEXT PRIMARY KEY,"
            f" embedding vector({dim}) NOT NULL)"
        )

    def store_embedding(self, chunk_id: str, vector: np.ndarray) -> None:
        v = np.asarray(vector, dtype=np.float32).ravel()
        if v.shape[0] != self.dim:
            raise ValueError(f"expected dim {self.dim}, got {v.shape[0]}")
        self._conn.run(
            "INSERT INTO chunk_embeddings (chunk_id, embedding)"
            " VALUES (:cid, CAST(:emb AS vector))"
            " ON CONFLICT (chunk_id) DO UPDATE SET embedding = excluded.embedding",
            cid=chunk_id,
            emb=_vec_literal(v),
        )

    def load_embeddings(self) -> tuple[list[str], np.ndarray]:
        rows = self._conn.run(
            "SELECT chunk_id, embedding FROM chunk_embeddings ORDER BY chunk_id"
        )
        if not rows:
            return [], np.empty((0, self.dim), dtype=np.float32)
        ids = [r[0] for r in rows]
        matrix = np.asarray(
            [np.array(str(r[1]).strip("[]").split(","), dtype=np.float32) for r in rows]
        )
        return ids, matrix

    def close(self) -> None:
        self._conn.close()


class PgVectorIndex:
    """The VectorIndex seam on pgvector: exact cosine via the <=> operator
    (cosine DISTANCE; similarity = 1 - distance), ordered in the database.
    Same call shape as storage.VectorIndex.search."""

    def __init__(self, store: PgEmbeddingStore):
        self._store = store

    def search(self, query: np.ndarray, top_k: int = 10) -> list[tuple[str, float]]:
        q = _vec_literal(query)
        rows = self._store._conn.run(
            "SELECT chunk_id, 1 - (embedding <=> CAST(:q AS vector)) AS cosine"
            " FROM chunk_embeddings"
            " ORDER BY embedding <=> CAST(:q AS vector)"
            " LIMIT :k",
            q=q,
            k=top_k,
        )
        return [(r[0], float(r[1])) for r in rows]


def migrate_embeddings(sqlite_storage: Storage, dsn: str) -> int:
    """Move every chunk embedding from a working SQLite database into
    PostgreSQL. Returns the number of vectors migrated. Idempotent
    (upserts)."""
    ids, matrix = sqlite_storage.load_embeddings()
    if not ids:
        return 0
    store = PgEmbeddingStore(dsn, dim=int(matrix.shape[1]))
    try:
        for chunk_id, vec in zip(ids, matrix):
            store.store_embedding(chunk_id, vec)
        store._conn.run("COMMIT")
    finally:
        store.close()
    return len(ids)
