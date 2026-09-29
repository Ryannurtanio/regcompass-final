"""Run Records: the saved facts of a Run.

Nothing here calls a paid model or touches the network. Every run goes through
the fake Engine (scripted answers, local gate), and the token arithmetic is
proved against a TEMPORARY registry whose fake Engine carries real prices: the
shipped fake Engine is priced 0/0 on purpose, so cost times any token count is
zero there and the assertion would be vacuous.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from regcompass.config import CONFIG_DIR
from regcompass.engines import fake_completion, fake_embed, resolve_engine
from regcompass.storage import Storage
from regcompass.server import create_app

from corpus_fixtures import seed_corpus  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _sg_corpus(storage: Storage, tmp_path: Path) -> Path:
    """A Run reads the Corpus, so a Run Record test needs one.
    Seeded the way Discovery builds it (tests/corpus_fixtures.py)."""
    data_dir = tmp_path / "data"
    seed_corpus(storage, data_dir, "SG")
    return data_dir

# Prices for the test-only Engine: deliberately not round numbers so a wrong
# multiplication cannot land on the right answer by accident.
TEST_USD_IN = 3.0
TEST_USD_OUT = 11.0


@pytest.fixture()
def no_ollama(monkeypatch):
    """Any attempt to embed through Ollama is a test failure: the fake Engine
    must carry the gate too."""
    import regcompass.gate as gate_mod

    def boom(*a, **k):
        raise AssertionError("the fake Engine must never reach Ollama")

    monkeypatch.setattr(gate_mod, "embed_ollama", boom)


@pytest.fixture()
def priced_config(tmp_path):
    """A copy of config/ whose registry adds a PRICED fake Engine. The shipped
    fake stays 0/0 (a demo that bills nothing must not claim a price)."""
    import yaml

    cfg = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, cfg)
    models = yaml.safe_load((cfg / "models.yaml").read_text(encoding="utf-8"))
    entry = dict(models["engines"]["fake"])
    entry["display_name"] = "Fake Engine (priced, tests only)"
    entry["usd_per_million_input_tokens"] = TEST_USD_IN
    entry["usd_per_million_output_tokens"] = TEST_USD_OUT
    models["engines"]["fake-priced"] = entry
    (cfg / "models.yaml").write_text(yaml.safe_dump(models), encoding="utf-8")
    return cfg


@pytest.fixture()
def db(tmp_path):
    storage = Storage(tmp_path / "runs.db")
    storage.apply_schema()
    return storage


def _rows(db_path) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY started_at")]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. the schema, and the record's two moments
# ---------------------------------------------------------------------------


class TestSchemaAndLifecycle:
    def test_a_fresh_database_has_the_runs_table(self, db):
        assert "runs" in db.table_names()

    def test_the_storage_methods_round_trip_a_record(self, db):
        from regcompass.storage import new_run_id, utc_now_z

        run_id = new_run_id("run")
        db.run_start(
            run_id=run_id, kind="run", economy="SG", pillars=[6, 7],
            indicators=["7.1", "7.2"], engine="fake", started_at=utc_now_z(),
        )
        opened = db.run_get(run_id)
        assert opened["status"] == "running"
        assert opened["pillars"] == [6, 7]
        assert opened["indicators"] == ["7.1", "7.2"]
        assert opened["ended_at"] is None

        db.run_finish(
            run_id, status="completed", ended_at=utc_now_z(),
            prompt_tokens=11, completion_tokens=3, cost_usd=0.5,
            details={"n_passed": 2},
        )
        done = db.run_get(run_id)
        assert done["status"] == "completed"
        assert (done["prompt_tokens"], done["completion_tokens"]) == (11, 3)
        assert done["cost_usd"] == 0.5
        assert done["provider_cost_usd"] is None
        assert done["details"] == {"n_passed": 2}
        assert done["documents_fetched"] == 0

    def test_an_unknown_id_is_none(self, db):
        assert db.run_get("run_nope") is None

    def test_runs_list_is_newest_first_and_filterable(self, db):
        db.run_start(
            run_id="run_a", kind="run", economy="SG", pillars=[7],
            indicators=None, engine="fake", started_at="2026-09-01T00:00:00.000000Z",
        )
        db.run_start(
            run_id="run_b", kind="run", economy="MY", pillars=[6],
            indicators=None, engine="fake", started_at="2026-09-02T00:00:00.000000Z",
        )
        db.run_start(
            run_id="disc_c", kind="discovery", economy="SG", pillars=[],
            indicators=None, engine=None, started_at="2026-09-03T00:00:00.000000Z",
        )
        assert [r["run_id"] for r in db.runs_list()] == ["disc_c", "run_b", "run_a"]
        assert [r["run_id"] for r in db.runs_list(economy="SG")] == ["disc_c", "run_a"]
        assert [r["run_id"] for r in db.runs_list(kind="run")] == ["run_b", "run_a"]
        assert [r["run_id"] for r in db.runs_list(limit=1)] == ["disc_c"]

    def test_a_run_is_running_at_the_start_and_completed_at_the_end(
        self, tmp_path, monkeypatch, no_ollama
    ):
        """The record exists BEFORE the first model call: a stage patched into
        the middle of the run reads it from its own connection."""
        import regcompass.pipeline as pipeline_mod

        monkeypatch.chdir(ROOT)
        path = tmp_path / "run.db"
        storage = Storage(path)
        storage.apply_schema()
        data_dir = _sg_corpus(storage, tmp_path)

        seen: list[dict] = []
        original = pipeline_mod.run_document

        def spy(*args, **kwargs):
            seen.extend(_rows(path))
            return original(*args, **kwargs)

        monkeypatch.setattr(pipeline_mod, "run_document", spy)
        report = pipeline_mod.run_economy(
            storage, "SG", (7,), resolve_engine("fake"), data_dir=data_dir,
            completion_fn=fake_completion, embed_fn=fake_embed,
        )

        assert seen, "run_document never ran"
        assert seen[0]["status"] == "running"
        assert seen[0]["ended_at"] is None
        assert seen[0]["run_id"] == report.run_id

        final = storage.run_get(report.run_id)
        assert final["status"] == "completed"
        assert final["kind"] == "run"
        assert final["economy"] == "SG"
        assert final["pillars"] == [7]
        assert final["engine"] == "fake"
        assert final["ended_at"] is not None
        assert final["error"] is None
        assert final["details"]["n_passed"] == report.n_passed

    def test_a_failing_run_leaves_a_failed_record_with_the_error(
        self, tmp_path, monkeypatch
    ):
        import regcompass.pipeline as pipeline_mod

        monkeypatch.chdir(ROOT)
        path = tmp_path / "boom.db"
        storage = Storage(path)
        storage.apply_schema()
        data_dir = _sg_corpus(storage, tmp_path)

        def boom(*args, **kwargs):
            raise RuntimeError("the extractor fell over")

        monkeypatch.setattr(pipeline_mod, "run_document", boom)
        with pytest.raises(RuntimeError):
            pipeline_mod.run_economy(
                storage, "SG", (7,), resolve_engine("fake"), data_dir=data_dir,
                completion_fn=fake_completion, embed_fn=fake_embed,
            )

        rows = _rows(path)
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        assert "the extractor fell over" in rows[0]["error"]
        assert rows[0]["ended_at"] is not None


# ---------------------------------------------------------------------------
# 2. tokens and cost: the arithmetic, asserted on the numbers
# ---------------------------------------------------------------------------


class TestTokensAndCost:
    def test_tokens_and_cost_match_the_fake_engines_scripted_usage(
        self, tmp_path, monkeypatch, no_ollama, priced_config
    ):
        from regcompass.pipeline import run_economy

        monkeypatch.chdir(ROOT)
        storage = Storage(tmp_path / "priced.db")
        storage.apply_schema()
        data_dir = _sg_corpus(storage, tmp_path)
        engine = resolve_engine("fake-priced", priced_config)
        assert engine.usd_per_million_input_tokens == TEST_USD_IN

        # Recompute the scripted rule independently of the meter: every answer
        # the fake Engine gives is counted here from the same prompt and text.
        counted: list[tuple[int, int]] = []

        def counting_fake(prompt: str, strict: bool) -> str:
            answer = fake_completion(prompt, strict)
            counted.append((len(prompt) // 4, len(answer) // 4))
            return answer

        report = run_economy(
            storage, "SG", (7,), engine, data_dir=data_dir,
            config_dir=priced_config,
            completion_fn=counting_fake, embed_fn=fake_embed,
        )

        assert counted, "the run made no model call"
        expected_in = sum(c[0] for c in counted)
        expected_out = sum(c[1] for c in counted)
        expected_cost = (
            expected_in * TEST_USD_IN / 1e6 + expected_out * TEST_USD_OUT / 1e6
        )
        assert expected_cost > 0

        assert report.prompt_tokens == expected_in
        assert report.completion_tokens == expected_out
        assert report.cost_usd == pytest.approx(expected_cost)

        record = storage.run_get(report.run_id)
        assert record["prompt_tokens"] == expected_in
        assert record["completion_tokens"] == expected_out
        assert record["cost_usd"] == pytest.approx(expected_cost)
        assert record["provider_cost_usd"] is None

    def test_documents_fetched_is_zero_on_every_run_record(
        self, tmp_path, monkeypatch, no_ollama
    ):
        from regcompass.pipeline import run_economy

        monkeypatch.chdir(ROOT)
        path = tmp_path / "fetch.db"
        storage = Storage(path)
        storage.apply_schema()
        data_dir = _sg_corpus(storage, tmp_path)
        run_economy(
            storage, "SG", (7,), resolve_engine("fake"), data_dir=data_dir,
            completion_fn=fake_completion, embed_fn=fake_embed,
        )
        rows = [r for r in _rows(path) if r["kind"] == "run"]
        assert rows
        assert all(r["documents_fetched"] == 0 for r in rows)

    def test_the_meter_is_run_scoped(self, monkeypatch):
        """Two meters in a row do not accumulate into one another."""
        from regcompass.engines import read_meter, start_meter

        start_meter()
        fake_completion("INDICATOR 7.2\n<<<PROVISION\n" + "x" * 80 + "\nPROVISION>>>", True)
        first = read_meter()
        assert first.calls == 1 and first.prompt_tokens > 0

        start_meter()
        second = read_meter()
        assert (second.calls, second.prompt_tokens, second.completion_tokens) == (0, 0, 0)

    def test_a_malformed_provider_cost_header_still_records_the_tokens(self):
        """The provider's cost figure is the optional cross-check; the tokens
        are the measurement. Nonsense in the header must not cost us both."""
        from regcompass.engines import _record_response_usage, read_meter, start_meter

        class _Usage:
            prompt_tokens = 700
            completion_tokens = 90

        class _Response:
            usage = _Usage()
            _hidden_params = {
                "additional_headers": {
                    "llm_provider-x-litellm-response-cost": "not-a-number"
                }
            }

        start_meter()
        _record_response_usage(_Response())
        totals = read_meter()
        assert (totals.prompt_tokens, totals.completion_tokens) == (700, 90)
        assert totals.calls == 1
        assert totals.provider_cost_usd is None

    def test_a_reported_provider_cost_is_summed(self):
        from regcompass.engines import _record_response_usage, read_meter, start_meter

        class _Usage:
            prompt_tokens = 10
            completion_tokens = 5

        class _Response:
            usage = _Usage()
            _hidden_params = {
                "additional_headers": {"llm_provider-x-litellm-response-cost": "0.0025"}
            }

        start_meter()
        _record_response_usage(_Response())
        _record_response_usage(_Response())
        totals = read_meter()
        assert totals.calls == 2
        assert totals.provider_cost_usd == pytest.approx(0.005)

    def test_cost_is_the_declared_price_times_the_tokens(self, priced_config):
        from regcompass.engines import cost_usd_for

        engine = resolve_engine("fake-priced", priced_config)
        assert cost_usd_for(engine, 1_000_000, 0) == pytest.approx(TEST_USD_IN)
        assert cost_usd_for(engine, 0, 1_000_000) == pytest.approx(TEST_USD_OUT)
        assert cost_usd_for(resolve_engine("fake"), 5_000, 5_000) == 0.0


# ---------------------------------------------------------------------------
# 3. the command line
# ---------------------------------------------------------------------------


@pytest.fixture()
def offline_cli(monkeypatch):
    from typer.testing import CliRunner

    import regcompass.cli as cli_mod

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(cli_mod, "_load_dotenv", lambda *a, **k: None)
    return CliRunner(), cli_mod.app


class TestCommandLine:
    def test_run_prints_the_run_record(
        self, tmp_path, monkeypatch, offline_cli, no_ollama
    ):
        runner, app = offline_cli
        monkeypatch.chdir(ROOT)
        path = tmp_path / "cli.db"
        storage = Storage(path)
        storage.apply_schema()
        data_dir = _sg_corpus(storage, tmp_path)
        storage.close()
        result = runner.invoke(
            app,
            ["run", "--economy", "SG", "--pillar", "7", "--engine", "fake",
             "--db", str(path), "--data-dir", str(data_dir)],
        )
        assert result.exit_code == 0, result.output
        out = result.output
        record = _rows(path)[0]
        assert "Run Record" in out
        assert record["run_id"] in out
        for label in ("Economy", "Pillar", "Engine", "status", "tokens", "cost",
                      "Documents fetched"):
            assert label in out, label
        assert "completed" in out
        # the pre-existing summary line survives (other tests read it)
        assert "engine=fake" in out and "verified" in out

    def test_runs_lists_newest_first_and_show_prints_one(
        self, tmp_path, offline_cli
    ):
        runner, app = offline_cli
        path = tmp_path / "list.db"
        storage = Storage(path)
        storage.apply_schema()
        storage.run_start(
            run_id="run_older", kind="run", economy="SG", pillars=[7],
            indicators=None, engine="fake", started_at="2026-09-01T00:00:00.000000Z",
        )
        storage.run_finish(
            "run_older", status="completed", ended_at="2026-09-01T00:01:00.000000Z",
            prompt_tokens=10, completion_tokens=2, cost_usd=0.25,
        )
        storage.run_start(
            run_id="run_newer", kind="run", economy="MY", pillars=[6],
            indicators=None, engine="fake", started_at="2026-09-02T00:00:00.000000Z",
        )
        storage.conn.close()

        listed = runner.invoke(app, ["runs", "--db", str(path)])
        assert listed.exit_code == 0, listed.output
        assert listed.output.index("run_newer") < listed.output.index("run_older")

        only_sg = runner.invoke(app, ["runs", "--db", str(path), "--economy", "SG"])
        assert "run_older" in only_sg.output and "run_newer" not in only_sg.output

        shown = runner.invoke(app, ["runs", "show", "run_older", "--db", str(path)])
        assert shown.exit_code == 0, shown.output
        record = json.loads(shown.stdout)
        assert record["run_id"] == "run_older"
        assert record["cost_usd"] == 0.25
        assert record["documents_fetched"] == 0

    def test_show_on_an_unknown_id_exits_nonzero(self, tmp_path, offline_cli):
        runner, app = offline_cli
        path = tmp_path / "empty.db"
        Storage(path).apply_schema()
        result = runner.invoke(app, ["runs", "show", "run_nope", "--db", str(path)])
        assert result.exit_code != 0
        assert "run_nope" in result.output

    def test_map_pdf_writes_a_run_record(
        self, tmp_path, monkeypatch, offline_cli, no_ollama
    ):
        """One user-supplied PDF is still a Run, so it lands on the same ledger.

        The export battery's verdict on this lane is not the Run Record's business
        (the fake Engine quotes the same provision for several Indicators of
        one Pillar, which the battery calls a duplicate row), so the test pins
        the record and requires only that its status tells the truth about how
        the command ended."""
        runner, app = offline_cli
        monkeypatch.chdir(ROOT)
        path = tmp_path / "mappdf.db"
        pdf = ROOT / "tests/fixtures/sample_legislation/born_digital/Telecommunications Act 1999.pdf"
        result = runner.invoke(
            app,
            ["map-pdf", "--pdf", str(pdf), "--economy", "SG", "--pillar", "7",
             "--engine", "fake", "--law-name", "Telecommunications Act 1999",
             "--source-url", "https://sso.agc.gov.sg/Act/TA1999",
             "--db", str(path), "--out", str(tmp_path / "out")],
        )
        rows = _rows(path)
        assert len(rows) == 1, result.output
        record = rows[0]
        assert record["kind"] == "run"
        assert record["economy"] == "SG"
        assert record["engine"] == "fake"
        assert record["documents_fetched"] == 0
        assert record["prompt_tokens"] > 0
        assert record["completion_tokens"] > 0
        # the shipped fake Engine is priced 0/0: it bills nothing, and says so
        assert record["cost_usd"] == 0.0
        if result.exit_code == 0:
            assert record["status"] == "completed"
            assert record["error"] is None
        else:
            assert record["status"] == "failed"
            assert record["error"]
        # either way the operator is told which record to look up
        assert record["run_id"] in result.output
        assert "Run Record" in result.output


# ---------------------------------------------------------------------------
# 4. the server
# ---------------------------------------------------------------------------


def _wait_done(client, timeout=180.0):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get("/api/status").json()
        if not st["active"] and st["status"] in ("done", "error"):
            return st
        time.sleep(0.05)
    raise AssertionError("run did not finish in time")


@pytest.fixture()
def seeded_app(tmp_path):
    """An app over a db holding two finished records."""
    path = tmp_path / "web.db"
    storage = Storage(path)
    storage.apply_schema()
    storage.run_start(
        run_id="run_one", kind="run", economy="SG", pillars=[7],
        indicators=["7.1"], engine="fake", started_at="2026-09-01T00:00:00.000000Z",
    )
    storage.run_finish(
        "run_one", status="completed", ended_at="2026-09-01T00:02:00.000000Z",
        prompt_tokens=120, completion_tokens=34, cost_usd=0.75,
        details={"n_passed": 4},
    )
    storage.run_start(
        run_id="disc_two", kind="discovery", economy="MY", pillars=[],
        indicators=None, engine=None, started_at="2026-09-02T00:00:00.000000Z",
    )
    storage.run_finish(
        "disc_two", status="completed", ended_at="2026-09-02T00:01:00.000000Z",
        documents_fetched=6, details={"strategy": "fixture"},
    )
    storage.conn.close()
    out = tmp_path / "out"
    out.mkdir()
    return TestClient(create_app(db_path=path, out_dir=out)), path


class TestServer:
    def test_list_is_newest_first_and_filterable(self, seeded_app):
        client, _ = seeded_app
        body = client.get("/api/runs").json()
        assert [r["run_id"] for r in body["runs"]] == ["disc_two", "run_one"]
        assert body["runs"][0]["kind"] == "discovery"
        assert body["runs"][0]["documents_fetched"] == 6

        only_runs = client.get("/api/runs", params={"kind": "run"}).json()["runs"]
        assert [r["run_id"] for r in only_runs] == ["run_one"]
        only_my = client.get("/api/runs", params={"economy": "MY"}).json()["runs"]
        assert [r["run_id"] for r in only_my] == ["disc_two"]
        assert len(client.get("/api/runs", params={"limit": 1}).json()["runs"]) == 1

    def test_get_one_and_404(self, seeded_app):
        client, _ = seeded_app
        record = client.get("/api/runs/run_one").json()
        assert record["economy"] == "SG"
        assert record["pillars"] == [7]
        assert record["indicators"] == ["7.1"]
        assert record["prompt_tokens"] == 120
        assert record["cost_usd"] == 0.75
        assert record["details"] == {"n_passed": 4}
        assert client.get("/api/runs/run_nope").status_code == 404

    def test_download_is_a_json_attachment(self, seeded_app):
        client, _ = seeded_app
        r = client.get("/api/runs/run_one/download")
        assert r.status_code == 200
        assert "application/json" in r.headers["content-type"]
        assert 'filename="run_one.json"' in r.headers["content-disposition"]
        assert json.loads(r.text) == client.get("/api/runs/run_one").json()
        assert client.get("/api/runs/run_nope/download").status_code == 404

    def test_an_empty_database_lists_nothing_instead_of_failing(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        client = TestClient(create_app(db_path=tmp_path / "missing.db", out_dir=out))
        assert client.get("/api/runs").json() == {"runs": [], "total": 0, "offset": 0}
        assert client.get("/api/runs/run_one").status_code == 404

    def test_a_run_started_through_the_api_survives_a_restart(
        self, tmp_path, monkeypatch, no_ollama
    ):
        monkeypatch.chdir(ROOT)
        path = tmp_path / "restart.db"
        storage = Storage(path)
        storage.apply_schema()
        data_dir = _sg_corpus(storage, tmp_path)
        storage.conn.close()
        out = tmp_path / "out"
        out.mkdir()

        client = TestClient(create_app(db_path=path, out_dir=out, data_dir=data_dir))
        started = client.post(
            "/api/run", json={"economy": "SG", "pillars": [7], "engine": "fake"}
        )
        assert started.status_code == 200, started.text
        assert _wait_done(client)["status"] == "done"
        run_id = client.get("/api/runs").json()["runs"][0]["run_id"]
        client.close()
        del client

        # a fresh app on the same database: the record is the memory
        restarted = TestClient(create_app(db_path=path, out_dir=out, data_dir=data_dir))
        listed = restarted.get("/api/runs").json()["runs"]
        assert [r["run_id"] for r in listed] == [run_id]
        record = restarted.get(f"/api/runs/{run_id}").json()
        assert record["status"] == "completed"
        assert record["engine"] == "fake"
        assert record["economy"] == "SG"
        assert record["documents_fetched"] == 0
        assert record["prompt_tokens"] > 0

    def test_stats_reports_the_last_run_record_not_a_flat_rate(self, seeded_app):
        client, _ = seeded_app
        stats = client.get("/api/stats").json()
        assert "cost_note" not in stats
        assert "cost" not in stats
        last = stats["last_run"]
        assert last["run_id"] == "run_one"
        assert last["prompt_tokens"] == 120
        assert last["completion_tokens"] == 34
        assert last["cost_usd"] == 0.75

    def test_stats_last_run_is_none_on_an_empty_database(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        client = TestClient(create_app(db_path=tmp_path / "nothing.db", out_dir=out))
        assert client.get("/api/stats").json()["last_run"] is None


# ---------------------------------------------------------------------------
# 5. the flat rate is gone
# ---------------------------------------------------------------------------


class TestFlatRateRemoved:
    def test_no_flat_rate_constant_or_wording_survives(self):
        targets = list((ROOT / "src" / "regcompass").rglob("*.py"))
        assert targets
        banned = ("_MEASURED_USD_PER_DOC", "_MEASURED_ENGINE", "0.04/document",
                  "$0.04", "per-document rate", "_cost_note", "_cost_block")
        offenders = []
        for path in targets:
            text = path.read_text(encoding="utf-8")
            for token in banned:
                if token in text:
                    offenders.append(f"{path.relative_to(ROOT)}: {token}")
        assert offenders == []

    def test_the_docs_no_longer_quote_the_flat_rate(self):
        for name in ("README.md", "docs/WEB_DEMO.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            assert "0.04 per\ndocument" not in text
            assert "~US$0.04" not in text
            assert "$0.04 per document" not in text
