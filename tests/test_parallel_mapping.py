"""M6 map + M7 verify with bounded concurrency.

Every test here is OFFLINE: the Engine is the fake one (or a stub built on it),
no key, no network, no Ollama. What is under test is the seam between the
worker threads, which may only call the Engine, and the main thread, which owns
storage, the audit trail and the record order.

The four properties asked for here:

1. wall time falls with concurrency, and the records still come out in PAIR
   order (the export is byte-identical only because of that);
2. a rate-limit answer is retried with backoff and counted on the Run Record;
3. one pair's hard failure costs that pair and nothing else;
4. the concurrency value comes from the Engine registry.
"""

from __future__ import annotations

import time

import pytest
import yaml

from regcompass import pipeline as pipeline_mod
from regcompass.config import CONFIG_DIR
from regcompass.contracts import Chunk, GatedChunk, PipelineConfig
from regcompass.engines import fake_completion, read_meter, resolve_engine, start_meter
from regcompass.pipeline import RunReport, map_and_verify_pairs, resolve_concurrency
from regcompass.storage import Storage

from corpus_fixtures import seed_corpus  # noqa: E402

FAKE = resolve_engine("fake")
CONFIG = PipelineConfig()
INDICATOR = "6.1"  # a plain PASS lane on the fake Engine (7.1 and 7.3 are scripted)


def pair_text(i: int) -> str:
    return (
        f"Section {i}. A licensee shall not operate a telecommunication network"
        f" without a written licence granted under this Act, numbered {i}.\n"
    )


def gated_pairs(n: int) -> list[GatedChunk]:
    """n passed (chunk, Indicator) pairs, each with its own provision text, so
    a record can be traced back to the pair that produced it."""
    pairs = []
    for i in range(1, n + 1):
        text = pair_text(i)
        pairs.append(
            GatedChunk(
                chunk=Chunk(
                    chunk_id=f"doc_par:c{i:04d}",
                    document_id="doc_par",
                    char_start=0,
                    char_end=len(text),
                    text=text,
                    section_label=f"s. {i}",
                    chunk_kind="section",
                    page_start=1,
                ),
                indicator_id=INDICATOR,  # type: ignore[arg-type]
                cosine_pillar=0.6,
                bm25_indicator=1.0,
                gate_decision="passed",
            )
        )
    return pairs


def slow_engine(delay: float = 0.05):
    """The fake Engine's scripted answer behind a deliberate delay: a stand-in
    for the 4.45 seconds a hosted Engine takes per call."""

    def completion(prompt: str, strict: bool) -> str:
        time.sleep(delay)
        return fake_completion(prompt, strict)

    return completion


def run_pairs(storage: Storage, pairs, completion_fn, concurrency: int, report=None):
    report = report or RunReport(economy="SG", engine=FAKE.name)
    records = map_and_verify_pairs(
        storage,
        pairs,
        doc_id="doc_par",
        economy="SG",
        engine=FAKE,
        config=CONFIG,
        report=report,
        completion_fn=completion_fn,
        concurrency=concurrency,
    )
    return records, report


@pytest.fixture()
def storage(tmp_path):
    st = Storage(tmp_path / "pairs.db")
    st.apply_schema()
    return st


class TestConcurrencyBuysWallTime:
    def test_four_workers_beat_one_by_three_times_and_keep_pair_order(self, storage):
        pairs = gated_pairs(40)
        started = time.perf_counter()
        sequential, seq_report = run_pairs(storage, pairs, slow_engine(), 1)
        sequential_s = time.perf_counter() - started

        started = time.perf_counter()
        parallel, par_report = run_pairs(storage, pairs, slow_engine(), 4)
        parallel_s = time.perf_counter() - started

        assert [r.chunk_id for r in sequential] == [p.chunk.chunk_id for p in pairs]
        assert [r.chunk_id for r in parallel] == [p.chunk.chunk_id for p in pairs]
        assert [r.verbatim_quote for r in parallel] == [
            r.verbatim_quote for r in sequential
        ]
        assert par_report.n_passed == seq_report.n_passed == 40
        assert parallel_s < sequential_s / 3

    def test_the_usage_meter_sees_the_worker_threads(self, storage):
        """The meter is a context variable, and a bare worker thread starts with
        an empty context: without copying the context into the worker, a
        parallel Run would report zero tokens and zero cost."""
        pairs = gated_pairs(8)
        start_meter()
        run_pairs(storage, pairs, fake_completion, 4)
        assert read_meter().calls == 8
        assert read_meter().prompt_tokens > 0

    def test_the_audit_trail_carries_one_m6_row_per_pair_in_order(self, storage):
        pairs = gated_pairs(12)
        run_pairs(storage, pairs, fake_completion, 4)
        rows = storage.conn.execute(
            "SELECT decision FROM audit_log WHERE stage = 'm6_map' ORDER BY id"
        ).fetchall()
        assert len(rows) == 12
        assert all(r["decision"].startswith("mapped") for r in rows)

    def test_the_m6_row_records_the_engine_time_not_the_consume_time(self, storage):
        """The call happens on a worker thread, so the with-block around the
        audit row measures nothing. The row must still carry the Engine's own
        wall time or the stage breakdown becomes a fiction."""
        run_pairs(storage, gated_pairs(4), slow_engine(0.05), 4)
        rows = storage.conn.execute(
            "SELECT duration_ms FROM audit_log WHERE stage = 'm6_map'"
        ).fetchall()
        assert all(r["duration_ms"] >= 40 for r in rows)


class TestRateLimitsAreRetriedAndCounted:
    def test_two_rate_limit_answers_then_success(self, storage, monkeypatch):
        waits: list[float] = []
        monkeypatch.setattr(pipeline_mod, "_sleep", waits.append)

        class RateLimitError(Exception):
            status_code = 429

        calls = {"n": 0}

        def completion(prompt: str, strict: bool) -> str:
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RateLimitError("429 rate limit exceeded")
            return fake_completion(prompt, strict)

        records, report = run_pairs(storage, gated_pairs(1), completion, 4)
        assert len(records) == 1
        assert records[0].verification_status == "passed"
        assert report.rate_limit_retries == 2
        assert waits == [0.5, 1.0]

    def test_a_rate_limit_that_never_clears_costs_only_its_own_pair(
        self, storage, monkeypatch
    ):
        monkeypatch.setattr(pipeline_mod, "_sleep", lambda s: None)

        class RateLimitError(Exception):
            status_code = 429

        def completion(prompt: str, strict: bool) -> str:
            if "numbered 3." in prompt:
                raise RateLimitError("429 rate limit exceeded")
            return fake_completion(prompt, strict)

        records, report = run_pairs(storage, gated_pairs(6), completion, 4)
        assert [r.chunk_id for r in records] == [
            "doc_par:c0001", "doc_par:c0002", "doc_par:c0004",
            "doc_par:c0005", "doc_par:c0006",
        ]
        # 5 attempts means 4 waits for the one pair that never cleared.
        assert report.rate_limit_retries == 4
        assert report.n_dropped == 1

    def test_the_transport_ladder_still_owns_the_other_failures(
        self, storage, monkeypatch
    ):
        """A dropped connection is not a rate limit: it keeps the 3-try
        transport ladder, and its waits are the transport ones."""
        waits: list[float] = []
        monkeypatch.setattr(pipeline_mod, "_sleep", waits.append)
        state = {"n": 0}

        def completion(prompt: str, strict: bool) -> str:
            state["n"] += 1
            if state["n"] == 1:
                raise ConnectionError("connection reset by peer")
            return fake_completion(prompt, strict)

        records, report = run_pairs(storage, gated_pairs(1), completion, 4)
        assert len(records) == 1
        assert report.rate_limit_retries == 0
        assert waits == [5]


class TestOnePairsFailureStaysOnePair:
    def test_thirty_nine_of_forty_records_survive_a_hard_failure(
        self, storage, monkeypatch
    ):
        monkeypatch.setattr(pipeline_mod, "_sleep", lambda s: None)

        def completion(prompt: str, strict: bool) -> str:
            if "numbered 7." in prompt:
                raise RuntimeError("the Engine hung up")
            return fake_completion(prompt, strict)

        records, report = run_pairs(storage, gated_pairs(40), completion, 4)
        assert len(records) == 39
        assert "doc_par:c0007" not in {r.chunk_id for r in records}
        assert report.n_dropped == 1
        assert any("c0007" in note for note in report.notes)

    def test_a_missing_key_still_stops_the_whole_run(self, storage, monkeypatch):
        """A ConfigError is not a blip: the pair loop must not swallow it into
        a per-pair skip and finish green over zero records. The audit trail
        still gets the m6 row that says why the Run stopped, which is what the
        one-at-a-time lane always wrote."""
        monkeypatch.setattr(pipeline_mod, "_sleep", lambda s: None)

        def completion(prompt: str, strict: bool) -> str:
            raise pipeline_mod.ConfigError("OPENROUTER_API_KEY is not set")

        with pytest.raises(pipeline_mod.ConfigError):
            run_pairs(storage, gated_pairs(8), completion, 4)
        rows = storage.conn.execute(
            "SELECT decision FROM audit_log WHERE stage = 'm6_map' ORDER BY id"
        ).fetchall()
        assert [r["decision"] for r in rows] == ["error: ConfigError"]

    def test_the_failing_pair_owns_the_error_row(self, storage, monkeypatch):
        """The row must belong to the pair that raised, not to the first one:
        the futures are read in pair order, so the third pair's key failure is
        the third row."""
        monkeypatch.setattr(pipeline_mod, "_sleep", lambda s: None)

        def completion(prompt: str, strict: bool) -> str:
            if "numbered 3." in prompt:
                raise pipeline_mod.ConfigError("OPENROUTER_API_KEY is not set")
            return fake_completion(prompt, strict)

        with pytest.raises(pipeline_mod.ConfigError):
            run_pairs(storage, gated_pairs(6), completion, 4)
        rows = storage.conn.execute(
            "SELECT input_hash, decision FROM audit_log WHERE stage = 'm6_map' ORDER BY id"
        ).fetchall()
        assert [r["decision"] for r in rows] == [
            "mapped (attempts 1)", "mapped (attempts 1)", "error: ConfigError",
        ]
        from regcompass.observability import sha256_hex

        assert rows[2]["input_hash"] == sha256_hex(f"doc_par:c0003::{INDICATOR}")


class TestTheExportIsByteIdenticalEitherWay:
    """The determinism gem, under concurrency: the same Corpus on the same
    Engine must ship the same bytes whether one call or four are in flight."""

    def submission_bytes(self, tmp_path, concurrency: int) -> bytes:
        from regcompass.engines import fake_embed
        from regcompass.pipeline import export_from_db, run_economy

        root = tmp_path / f"c{concurrency}"
        root.mkdir(parents=True, exist_ok=True)
        storage = Storage(root / "corpus.db")
        storage.apply_schema()
        seed_corpus(storage, root / "data", "MY")
        report = run_economy(
            storage, "MY", (7,), FAKE, data_dir=root / "data",
            completion_fn=fake_completion, embed_fn=fake_embed,
            concurrency=concurrency,
        )
        assert report.engine_concurrency == concurrency
        result = export_from_db(storage, root / "out", run_id=report.run_id)
        return result.csv_path.read_bytes()

    def test_four_workers_ship_the_same_csv_as_one(self, tmp_path):
        assert self.submission_bytes(tmp_path, 4) == self.submission_bytes(tmp_path, 1)


class TestTheRunRecordCarriesTheSetting:
    def test_run_document_records_the_value_it_resolved(self, tmp_path):
        """The map-pdf lane calls run_document directly, with no run_economy
        above it to stamp the report. Its Run Record must still name the
        concurrency the Run actually used, not the default 1."""
        from regcompass.engines import fake_embed
        from regcompass.fixtures import BUNDLED
        from regcompass.pipeline import RunReport, run_document

        engine = FAKE.model_copy(update={"concurrency": 4})
        storage = Storage(tmp_path / "one.db")
        storage.apply_schema()
        report = RunReport(economy="MY", engine=engine.name)
        run_document(
            storage, "doc_one", BUNDLED["MY"][0].path, "MY", (7,), engine, "run_one",
            indicators=("7.1",), report=report, completion_fn=fake_completion,
            embed_fn=fake_embed, language="English",
        )
        assert report.engine_concurrency == 4

    def test_details_name_the_concurrency_and_the_rate_limit_retries(self):
        from regcompass.pipeline import run_details

        report = RunReport(economy="AU", engine="engine-a")
        report.engine_concurrency = 4
        report.rate_limit_retries = 3
        details = run_details(report)
        assert details["engine_concurrency"] == 4
        assert details["rate_limit_retries"] == 3


class TestTheRegistryDeclaresConcurrency:
    def test_every_declared_engine_has_a_concurrency_value(self):
        raw = yaml.safe_load((CONFIG_DIR / "models.yaml").read_text(encoding="utf-8"))
        for name, entry in raw["engines"].items():
            assert "concurrency" in entry, f"Engine '{name}' declares no concurrency"
            assert entry["concurrency"] >= 1

    def test_the_fake_engine_runs_one_call_at_a_time(self):
        assert resolve_engine("fake").concurrency == 1

    def test_the_hosted_engines_run_four(self):
        assert resolve_engine("engine-a").concurrency == 4
        assert resolve_engine("engine-b").concurrency == 4

    def test_an_override_beats_the_registry_and_is_bounded(self):
        assert resolve_concurrency(FAKE) == 1
        assert resolve_concurrency(FAKE, 6) == 6
        assert resolve_concurrency(FAKE, 0) == 1
        assert resolve_concurrency(FAKE, 9999) == pipeline_mod.MAX_CONCURRENCY
