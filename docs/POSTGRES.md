# PostgreSQL + pgvector: the production storage swap

SQLite + numpy exact cosine is the SHIPPED default and the right call at
Round-1 scale: a few thousand 1024-dim vectors scan in one matmul,
sub-millisecond, in a single keyless offline file that doubles as the audit
artifact. This document is the swap story for when that stops being true
(concurrent writers, network access, corpora of 10^5+ vectors needing a real
ANN index) - and it is real code, not prose:

- `src/regcompass/pg.py` - `PgEmbeddingStore` and `PgVectorIndex` implement
  the same two seams `storage.py` defines (the embedding store and
  `VectorIndex.search(query, top_k)`), plus `migrate_embeddings()` which
  moves a working SQLite database's vectors over in one call.
- `docs/schema_postgres.sql` - the DDL variant (SERIAL / BOOLEAN / a
  `vector(1024)` column for embeddings).
- `tests/test_pg.py` - the parity suite: identical top-k results and cosine
  scores (to 1e-5) against the shipped numpy index over the same vectors. It
  runs whenever `REGCOMPASS_PG_DSN` is set and skips otherwise.

## Trying it

```bash
brew install postgresql@17 pgvector          # or your platform's packages
pg_ctl -D "$(brew --prefix)/var/postgresql@17" start
createdb regcompass_test
uv sync --extra postgres                     # adds pg8000 (BSD, pure Python)
REGCOMPASS_PG_DSN=postgresql://localhost/regcompass_test uv run pytest tests/test_pg.py -v
```

Driver note: the extra installs `pg8000` (BSD). The psycopg family is LGPL,
which is below this project's Apache-2.0-compatible license floor, so it is
deliberately not used. Vectors travel as pgvector text literals; no
driver-side type plugin is required.

## Scaling ladder

1. SQLite + numpy exact scan - shipped default, correct to ~10^4 vectors.
2. faiss-cpu (MIT) behind the same VectorIndex seam - in-process ANN when
   the corpus outgrows brute force (~10^5+).
3. PostgreSQL + pgvector 0.8.x (PostgreSQL License) - this document:
   concurrent writers, network access, HNSW index (commented in the DDL).

## No-Ollama fallback engine (documented alternate)

Judges who cannot run the Ollama daemon can serve the same pinned GGUF
weights with `llama-cpp-python 0.3.32` (MIT):

- Constrained output: use a raw GBNF grammar (the strongest schema guarantee
  a 4B model gets). Do NOT use its OpenAI-compatible chat endpoint's
  `response_format=json_schema` path - open bug ggml-org/llama.cpp#11847
  ("Either json_schema or grammar can be specified, but not both"); the same
  GGUF works on the non-chat `/completions` endpoint with a grammar.
- Install caveat: sdist-only on PyPI (needs a C toolchain); prebuilt CPU
  wheels exist only on the project's own index. This is why Ollama (prebuilt
  binary, zero compile) is the default engine.
- Wire-up: add an Engine entry in `config/models.yaml` pointing at the
  llama.cpp server's OpenAI-compatible endpoint (LiteLLM `openai/` prefix with
  a local api_base) and keep `num_retries: 0`.
