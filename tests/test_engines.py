"""The Engine registry: one named Engine drives every model-calling stage.

Everything here is OFFLINE: the fake Engine answers from the prompt itself, the
key preflight is exercised with the variable deleted, and `_load_dotenv` is
monkeypatched to a no-op wherever the CLI runs so a developer's real .env is
never read. No test in this file may reach OpenRouter or Ollama.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from regcompass.config import CONFIG_DIR, load_models
from regcompass.contracts import Engine, ModelsConfig, UnknownEngine
from regcompass.storage import Storage

from corpus_fixtures import seed_corpus  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_ENGINE_FIELDS = [
    "display_name",
    "litellm_model",
    "provider",
    "api_key_env",
    "usd_per_million_input_tokens",
    "usd_per_million_output_tokens",
    "open_weights",
    "structured_output",
    "concurrency",
]


def raw_models() -> dict:
    return yaml.safe_load((CONFIG_DIR / "models.yaml").read_text(encoding="utf-8"))


def fake_engine(**overrides) -> Engine:
    """An Engine object built inline, never loaded from config: the tests that
    prove no stage falls back to a hard-coded block must not touch the loader."""
    base = dict(
        name="fake",
        display_name="Fake Engine (offline)",
        litellm_model="fake/scripted",
        provider="fake",
        api_key_env=None,
        usd_per_million_input_tokens=0,
        usd_per_million_output_tokens=0,
        open_weights=True,
        structured_output="json_schema",
        concurrency=1,
    )
    base.update(overrides)
    return Engine(**base)


# ---------------------------------------------------------------------------
# 1. config validation
# ---------------------------------------------------------------------------


class TestRegistryValidation:
    @pytest.mark.parametrize("field", REQUIRED_ENGINE_FIELDS)
    def test_missing_required_field_fails(self, field):
        raw = copy.deepcopy(raw_models())
        raw["engines"]["engine-a"].pop(field)
        with pytest.raises(ValidationError):
            ModelsConfig(**raw)

    def test_default_engine_must_exist(self):
        raw = copy.deepcopy(raw_models())
        raw["default_engine"] = "engine-z"
        with pytest.raises(ValidationError, match="engine-z"):
            ModelsConfig(**raw)

    def test_transport_retries_pinned_zero(self):
        raw = copy.deepcopy(raw_models())
        raw["engines"]["engine-b"]["num_retries"] = 1
        with pytest.raises(ValidationError):
            ModelsConfig(**raw)

    def test_embedder_is_not_an_engine_name(self):
        raw = copy.deepcopy(raw_models())
        raw["engines"]["embedder"] = copy.deepcopy(raw["engines"]["fake"])
        with pytest.raises(ValidationError, match="shared embedder"):
            ModelsConfig(**raw)

    def test_engine_names_are_listed(self):
        assert load_models().engine_names == ["engine-a", "engine-b", "fake"]

    def test_name_is_injected_from_the_key(self):
        models = load_models()
        for name, engine in models.engines.items():
            assert engine.name == name

    def test_unknown_engine_lists_the_valid_names(self):
        models = load_models()
        with pytest.raises(UnknownEngine) as exc:
            models.engine("nope")
        message = str(exc.value)
        for name in ("engine-a", "engine-b", "fake"):
            assert name in message

    def test_the_declared_pair_differs_in_kind(self):
        """Engine A is the commercial hosted model, Engine B the
        open-weights one. The Comparison is meaningless if both are the same."""
        models = load_models()
        assert models.engine("engine-a").open_weights is False
        assert models.engine("engine-b").open_weights is True
        assert models.default_engine == "engine-b"

    def test_keys_are_named_never_carried(self):
        for engine in load_models().engines.values():
            if engine.api_key_env is not None:
                assert engine.api_key_env.isupper()
                assert not engine.api_key_env.startswith("sk-")


class TestResolveEngine:
    def test_none_means_the_default(self):
        from regcompass.engines import resolve_engine

        assert resolve_engine(None).name == load_models().default_engine

    def test_unknown_name_raises(self):
        from regcompass.engines import resolve_engine

        with pytest.raises(UnknownEngine, match="engine-a"):
            resolve_engine("engine-zzz")


class TestPreflightKey:
    def test_keyless_engine_needs_nothing(self):
        from regcompass.engines import preflight_key

        preflight_key(fake_engine())  # must not raise

    def test_missing_key_names_the_engine_and_the_variable(self, monkeypatch):
        from regcompass.engines import ConfigError, preflight_key

        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with pytest.raises(ConfigError) as exc:
            preflight_key(load_models().engine("engine-a"))
        message = str(exc.value)
        assert "engine-a" in message
        assert "GPT-5.6 Luna" in message
        assert "OPENROUTER_API_KEY" in message

    def test_present_key_passes_and_is_never_echoed(self, monkeypatch):
        from regcompass.engines import preflight_key

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-value-1234")
        preflight_key(load_models().engine("engine-b"))  # must not raise


# ---------------------------------------------------------------------------
# 2. a whole run on the fake Engine: no key, no network
# ---------------------------------------------------------------------------


@pytest.fixture()
def offline_cli(monkeypatch):
    """A CliRunner whose environment has no key and whose .env is never read."""
    from typer.testing import CliRunner

    import regcompass.cli as cli_mod

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(cli_mod, "_load_dotenv", lambda *a, **k: None)
    return CliRunner(), cli_mod.app


@pytest.fixture()
def no_ollama(monkeypatch):
    """Any attempt to embed through Ollama is a test failure: the fake Engine
    must carry the gate too."""
    import regcompass.gate as gate_mod

    def boom(*a, **k):
        raise AssertionError("the fake Engine must never reach Ollama")

    monkeypatch.setattr(gate_mod, "embed_ollama", boom)


class TestFakeEngineRun:
    def test_run_engine_fake_completes_offline(
        self, tmp_path, monkeypatch, offline_cli, no_ollama
    ):
        runner, app = offline_cli
        monkeypatch.chdir(ROOT)  # config/ resolution
        db = tmp_path / "db.sqlite"
        data = tmp_path / "data"
        storage = Storage(db)
        storage.apply_schema()
        seed_corpus(storage, data, "SG")
        storage.close()
        r = runner.invoke(
            app,
            [
                "run", "--economy", "SG", "--pillar", "7",
                "--engine", "fake", "--db", str(db), "--data-dir", str(data),
            ],
        )
        assert r.exit_code == 0, r.output
        assert "engine=fake" in r.output
        assert "documents" in r.output and "verified" in r.output

        records = Storage(db).load_mappings(economy="SG")
        assert records, "the fake Engine run must persist mappings"
        assert any(rec.verification_status == "passed" for rec in records)


class TestEngineOfRecord:
    """The export names the Engine that ACTUALLY ran, read off the audit trail
    the run wrote, never the registry menu."""

    def test_a_run_stamps_its_engine_where_the_export_reads_it(
        self, tmp_path, monkeypatch, no_ollama
    ):
        from regcompass.engines import fake_completion, fake_embed, resolve_engine
        from regcompass.pipeline import engine_of_record, run_economy
        from regcompass.storage import Storage

        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        storage = Storage(tmp_path / "db.sqlite")
        storage.apply_schema()
        seed_corpus(storage, tmp_path / "data", "SG")
        run_economy(
            storage, "SG", (7,), resolve_engine("fake"), data_dir=tmp_path / "data",
            completion_fn=fake_completion, embed_fn=fake_embed,
        )
        assert engine_of_record(storage).name == "fake"

    def test_export_metadata_names_only_that_engine(self, tmp_path):
        """The registry is a menu; run_metadata is a record. An Engine that did
        not answer must not appear in it."""
        import json
        import sys

        sys.path.insert(0, str(ROOT / "tests"))
        from test_export import COVERAGE, SLUGS, chunk_texts, gate_cosines, load_golden_m8

        from regcompass.engines import resolve_engine
        from regcompass.export import export_all

        records, texts, cosines = [], {}, {}
        for slug in SLUGS:
            records.extend(load_golden_m8(slug))
            texts.update(chunk_texts(slug))
            cosines.update(gate_cosines(slug))
        result = export_all(
            tmp_path / "out", records,
            chunk_text_lookup=texts, gate_cosine_lookup=cosines,
            coverage_stats=COVERAGE, liveness_fn=lambda url: True,
            engine=resolve_engine("fake"),
        )
        models = json.loads(result.submission_path.read_text(encoding="utf-8"))[
            "run_metadata"
        ]["models"]
        assert set(models) == {"engine", "embedder"}
        assert models["engine"]["name"] == "fake"
        assert "engine-a" not in json.dumps(models)
        assert "engine-b" not in json.dumps(models)

    def test_a_database_without_the_audit_stamp_falls_back_to_the_default(self, tmp_path):
        from regcompass.pipeline import engine_of_record
        from regcompass.storage import Storage

        storage = Storage(tmp_path / "db.sqlite")
        storage.apply_schema()
        assert engine_of_record(storage).name == load_models().default_engine


# ---------------------------------------------------------------------------
# 3. key preflight: before any model call, before the database
# ---------------------------------------------------------------------------


TELECOM = "tests/fixtures/sample_legislation/born_digital/Telecommunications Act 1999.pdf"


class TestKeyPreflight:
    def _assert_refused(self, result, db: Path, engine_name: str):
        assert result.exit_code == 2, result.output
        assert engine_name in result.output
        assert "OPENROUTER_API_KEY" in result.output
        assert not db.exists(), "preflight must precede opening the database"

    @pytest.mark.parametrize("engine_name", ["engine-a", "engine-b"])
    def test_run_without_the_key(self, tmp_path, offline_cli, engine_name):
        runner, app = offline_cli
        db = tmp_path / "db.sqlite"
        r = runner.invoke(
            app, ["run", "--economy", "SG", "--engine", engine_name, "--db", str(db)]
        )
        self._assert_refused(r, db, engine_name)

    def test_run_names_the_engine_display_name(self, tmp_path, offline_cli):
        runner, app = offline_cli
        r = runner.invoke(
            app,
            ["run", "--economy", "SG", "--engine", "engine-a", "--db", str(tmp_path / "db.sqlite")],
        )
        assert "GPT-5.6 Luna" in r.output

    def test_e2e_without_the_key(self, tmp_path, offline_cli):
        runner, app = offline_cli
        db = tmp_path / "db.sqlite"
        r = runner.invoke(
            app, ["e2e", "--economy", "SG", "--engine", "engine-a", "--db", str(db)]
        )
        self._assert_refused(r, db, "engine-a")

    def test_map_pdf_without_the_key(self, tmp_path, monkeypatch, offline_cli):
        runner, app = offline_cli
        monkeypatch.chdir(ROOT)
        db = tmp_path / "db.sqlite"
        r = runner.invoke(
            app,
            [
                "map-pdf", "--pdf", TELECOM, "--economy", "SG",
                "--engine", "engine-a", "--law-name", "Telecommunications Act 1999",
                "--source-url", "https://sso.agc.gov.sg/Act/TA1999",
                "--db", str(db),
            ],
        )
        self._assert_refused(r, db, "engine-a")

    def test_unknown_engine_lists_the_names(self, tmp_path, offline_cli):
        runner, app = offline_cli
        r = runner.invoke(
            app, ["run", "--economy", "SG", "--engine", "nope", "--db", str(tmp_path / "db.sqlite")]
        )
        assert r.exit_code == 2
        for name in ("engine-a", "engine-b", "fake"):
            assert name in r.output


# ---------------------------------------------------------------------------
# 4. no stage falls back to a hard-coded block
# ---------------------------------------------------------------------------


@pytest.fixture()
def no_loader(monkeypatch):
    """Every stage must use the Engine it is given. Any stage that still loads
    config behind the caller's back fails loudly here."""
    import regcompass.config as config_mod
    import regcompass.engines as engines_mod

    def boom(*a, **k):
        raise AssertionError("load_models must not be called")

    monkeypatch.setattr(engines_mod, "load_models", boom)
    monkeypatch.setattr(config_mod, "load_models", boom)


class TestNoHardCodedFallback:
    def test_run_economy_uses_only_the_given_engine(self, tmp_path, no_loader, no_ollama):
        from regcompass.pipeline import run_economy
        from regcompass.storage import Storage

        storage = Storage(tmp_path / "db.sqlite")
        storage.apply_schema()
        seed_corpus(storage, tmp_path / "data", "SG")
        report = run_economy(
            storage, "SG", (7,), fake_engine(), data_dir=tmp_path / "data",
            completion_fn=None, embed_fn=None,
        )
        assert report.engine == "fake"
        assert report.n_passed > 0
        assert report.n_dropped > 0
        assert report.n_no_evidence > 0

    def test_classify_record_uses_only_the_given_engine(self, no_loader):
        from regcompass.classify import classify_record

        record = _passed_record()
        out = classify_record(
            record, "A licensee shall retain the data for six years.",
            engine=fake_engine(),
            indicator_defs=_indicator_defs(),
        )
        assert out.outcome in ("classified", "unclassified")

    def test_reconcile_group_uses_only_the_given_engine(self, no_loader):
        from regcompass.reconcile import reconcile_group

        a = _passed_record(mapping_id="doc:c1::7.3")
        b = _passed_record(mapping_id="doc:c2::7.3")
        group, updated = reconcile_group(
            [a, b], {"doc_sg_x": "Telecommunications Act 1999"}, engine=fake_engine()
        )
        assert group.authoritative_mapping_id in {a.mapping_id, b.mapping_id}
        assert len(updated) == 2

    def test_llm_boundaries_uses_only_the_given_engine(self, no_loader):
        from regcompass.chunk import llm_boundaries
        from regcompass.contracts import CanonicalText, PageSpan

        text = "1. A licensee shall keep records.\n2. The Authority may inspect them.\n"
        canonical = CanonicalText(
            document_id="doc_sg_x",
            full_text=text,
            pages=[PageSpan(page_number=1, char_start=0, char_end=len(text))],
            extractor="test",
            extractor_version="0",
            source_sha256="a" * 64,
        )
        assert llm_boundaries(canonical, engine=fake_engine()) == []


def _indicator_defs():
    from regcompass.config import load_indicators

    return load_indicators()


def _passed_record(mapping_id: str = "doc_sg_x:c0001::7.3"):
    from regcompass.contracts import MappingRecord

    return MappingRecord(
        mapping_id=mapping_id,
        document_id="doc_sg_x",
        chunk_id=mapping_id.split("::")[0],
        economy="SG",
        indicator_id="7.3",
        indicator_name="Data retention requirements",
        section="s. 5",
        verbatim_quote="A licensee shall retain the data for six years.",
        verification_status="passed",
        extraction_attempts=1,
    )


# ---------------------------------------------------------------------------
# 5. the server: an Engine name in the run request
# ---------------------------------------------------------------------------


@pytest.fixture()
def web(tmp_path, monkeypatch):
    """The demo app over a scratch DB, with no key in the environment."""
    from fastapi.testclient import TestClient

    from regcompass.storage import Storage
    from regcompass.server import create_app

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    db = tmp_path / "web.db"
    data = tmp_path / "data"
    storage = Storage(db)
    storage.apply_schema()
    seed_corpus(storage, data, "SG")  # a Run reads the Corpus; give it one
    storage.conn.close()
    out = tmp_path / "out"
    out.mkdir()
    return TestClient(create_app(db_path=db, out_dir=out, data_dir=data)), db


def _wait_done(client, timeout=120.0):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get("/api/status").json()
        if not st["active"] and st["status"] in ("done", "error"):
            return st
        time.sleep(0.05)
    raise AssertionError("run did not finish in time")


class TestServerEngineSelection:
    def test_unknown_engine_is_a_400_listing_the_names(self, web):
        client, _ = web
        r = client.post("/api/run", json={"economy": "SG", "engine": "nope"})
        assert r.status_code == 400
        detail = r.json()["detail"]
        for name in ("engine-a", "engine-b", "fake"):
            assert name in detail

    def test_keyed_engine_without_its_key_is_a_400(self, web):
        client, _ = web
        r = client.post("/api/run", json={"economy": "SG", "engine": "engine-a"})
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert "engine-a" in detail
        assert "OPENROUTER_API_KEY" in detail

    def test_no_engine_named_means_the_configured_default(self, web):
        """The server's `tier` grace path retired with the one-page
        demo; the CLI's --tier alias lives on below. A request that
        names no Engine gets the configured default, which is keyed, so the
        refusal names that Engine and its variable."""
        client, _ = web
        r = client.post("/api/run", json={"economy": "SG"})
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert "engine-b" in detail and "OPENROUTER_API_KEY" in detail

    def test_engine_fake_runs_the_real_pipeline(self, web, monkeypatch, no_ollama):
        client, db = web
        monkeypatch.chdir(ROOT)
        r = client.post(
            "/api/run", json={"economy": "SG", "pillars": [7], "engine": "fake"}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["engine"] == "fake"
        st = _wait_done(client)
        assert st["status"] == "done", st

        from regcompass.storage import Storage

        records = Storage(db).load_mappings(economy="SG")
        assert records, "the fake Engine lane must write records to the working db"


# ---------------------------------------------------------------------------
# 6. the deprecated --tier alias
# ---------------------------------------------------------------------------


class TestDeprecatedTierFlag:
    def test_byok_warns_and_selects_engine_b(self, tmp_path, offline_cli):
        runner, app = offline_cli
        r = runner.invoke(
            app, ["run", "--economy", "SG", "--tier", "byok", "--db", str(tmp_path / "db.sqlite")]
        )
        assert r.exit_code == 2
        assert "--tier is deprecated; use --engine engine-b" in r.output
        assert "engine-b" in r.output
        assert "OPENROUTER_API_KEY" in r.output

    def test_local_is_retired(self, tmp_path, offline_cli):
        runner, app = offline_cli
        r = runner.invoke(
            app, ["run", "--economy", "SG", "--tier", "local", "--db", str(tmp_path / "db.sqlite")]
        )
        assert r.exit_code == 2
        assert "retired" in r.output

    def test_any_other_tier_value_is_refused(self, tmp_path, offline_cli):
        runner, app = offline_cli
        r = runner.invoke(
            app, ["run", "--economy", "SG", "--tier", "wizard", "--db", str(tmp_path / "db.sqlite")]
        )
        assert r.exit_code == 2

    def test_engine_wins_over_the_alias(self, tmp_path, monkeypatch, offline_cli, no_ollama):
        runner, app = offline_cli
        monkeypatch.chdir(ROOT)
        db = tmp_path / "db.sqlite"
        data = tmp_path / "data"
        storage = Storage(db)
        storage.apply_schema()
        seed_corpus(storage, data, "SG")
        storage.close()
        r = runner.invoke(
            app,
            [
                "run", "--economy", "SG", "--pillar", "7", "--engine", "fake",
                "--tier", "byok", "--db", str(db), "--data-dir", str(data),
            ],
        )
        assert r.exit_code == 0, r.output
        assert "--tier is deprecated" in r.output
        assert "engine=fake" in r.output


# ---------------------------------------------------------------------------
# 7. `regcompass engines`
# ---------------------------------------------------------------------------


class TestEnginesCommand:
    def test_lists_every_engine_and_marks_the_default(self, offline_cli):
        runner, app = offline_cli
        r = runner.invoke(app, ["engines"])
        assert r.exit_code == 0, r.output
        for name in ("engine-a", "engine-b", "fake"):
            assert name in r.output
        assert "engine-b (default)" in r.output
        assert "key: none needed" in r.output  # the fake Engine
        assert "OPENROUTER_API_KEY NOT SET" in r.output

    def test_never_prints_the_key_value(self, monkeypatch, offline_cli):
        runner, app = offline_cli
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-value-1234")
        r = runner.invoke(app, ["engines"])
        assert r.exit_code == 0, r.output
        assert "sk-or-test-value-1234" not in r.output
        assert "OPENROUTER_API_KEY set" in r.output


class TestLiteLLMIsQuiet:
    def test_a_call_on_an_unlisted_model_prints_no_provider_list_banner(
        self, capfd, monkeypatch
    ):
        """LiteLLM prices each answer against its own model list; the Qwen
        model on OpenRouter is not on it, and the failed lookup used to print a
        red "Provider List" banner several times per call into the server log."""
        from regcompass.engines import make_completion, resolve_engine

        engine = resolve_engine("engine-b")
        # litellm's mock lane answers offline; the key only has to be present
        monkeypatch.setenv(engine.api_key_env, "sk-offline-mock")
        completion = make_completion(engine, None, mock_response='{"ok": true}')
        assert completion("hello", True) == '{"ok": true}'
        captured = capfd.readouterr()
        assert "Provider List" not in captured.out + captured.err
