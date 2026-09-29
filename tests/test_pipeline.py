"""P0 judge-path pipeline: `regcompass run` / `regcompass export` wiring.

The full storage-attached lane runs OFFLINE here on the fake Engine: a
deterministic fake embedder (unit vectors) and a prompt-parsing fake completion
drive M5/M6/M7, so the test exercises the real stitch (shared attempt budget,
retry-and-drop, no-evidence, M8 ladder fallback, audit_log rows) without Ollama
or a key. Both fakes live in regcompass.engines (they are the fake Engine's own
behaviour, not a test fixture bolted on): per indicator the completion returns a
VERBATIM quote parsed out of the prompt's own <<<PROVISION block (passes M7
byte-for-byte), a quote that is NOT in the document (exhausts the budget: the
DROP lane), or maps_to_indicator=false (the no-evidence lane). For M8 group
prompts it returns garbage, so reconciliation falls back to the legal-hierarchy
ladder, the RULE-wins lane."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from regcompass.contracts import MappingRecord
from regcompass.engines import (
    DROP_INDICATOR,
    NO_EVIDENCE_INDICATOR,
    _INDICATOR_RE,
    _PROVISION_RE,
    fake_completion,
    fake_embed,
    resolve_engine,
)
from regcompass.pipeline import (
    EmptyCorpusError,
    export_from_db,
    run_economy,
)
from regcompass.storage import Storage

from corpus_fixtures import BUNDLED, seed_corpus  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

__all__ = [
    "DROP_INDICATOR",
    "NO_EVIDENCE_INDICATOR",
    "_INDICATOR_RE",
    "_PROVISION_RE",
    "fake_completion",
    "fake_embed",
]


@pytest.fixture(scope="module")
def sg_run(tmp_path_factory):
    """A Run over a seeded SG Corpus. The Corpus is built the way Discovery
    builds one (tests/corpus_fixtures.py), so the Run reads Documents rather
    than a list of fixtures baked into the code."""
    root = tmp_path_factory.mktemp("p0")
    storage = Storage(root / "regcompass.db")
    storage.apply_schema()
    seed_corpus(storage, root / "data", "SG")
    report = run_economy(
        storage, "SG", pillars=(7,), engine=resolve_engine("fake"),
        completion_fn=fake_completion, embed_fn=fake_embed, data_dir=root / "data",
    )
    return storage, report


class TestBundledFixtures:
    def test_every_bundled_fixture_is_committed(self):
        for economy, docs in BUNDLED.items():
            for doc in docs:
                assert doc.path.exists(), (economy, doc.filename_hint)

    def test_a_run_without_a_corpus_says_to_discover_first(self, tmp_path):
        storage = Storage(tmp_path / "empty.db")
        storage.apply_schema()
        with pytest.raises(EmptyCorpusError, match="regcompass discover"):
            run_economy(
                storage, "SG", pillars=(7,), engine=resolve_engine("fake"),
                completion_fn=fake_completion, embed_fn=fake_embed,
                data_dir=tmp_path / "data",
            )


class TestRunLane:
    def test_all_three_lanes_exercised(self, sg_run):
        _, report = sg_run
        assert report.n_pairs_gated > 0
        assert report.n_passed > 0, "verbatim-quote lane must pass"
        assert report.n_dropped > 0, "invented-quote lane must exhaust the budget and drop"
        assert report.n_no_evidence > 0, "maps_to_indicator=false lane must be recorded"

    def test_records_persisted_with_statuses(self, sg_run):
        storage, _ = sg_run
        records = storage.load_mappings(economy="SG")
        assert any(r.verification_status == "passed" for r in records)
        assert any(r.insufficient_evidence for r in records)
        # M6 drops carry NO record by design (nothing unverified ships);
        # their trail is the audit_log, asserted below
        assert not any(r.verification_status == "dropped" for r in records)

    def test_dropped_pairs_burned_the_full_attempt_budget_in_the_audit_log(self, sg_run):
        storage, _ = sg_run
        rows = storage.conn.execute(
            "SELECT decision FROM audit_log WHERE stage = 'm6_map'"
            " AND decision LIKE 'dropped%'"
        ).fetchall()
        assert rows, "the invented-quote lane must leave dropped m6_map audit rows"
        assert all("attempts 3" in r["decision"] for r in rows)

    def test_audit_log_covers_every_stage(self, sg_run):
        storage, _ = sg_run
        stages = {
            r["stage"]
            for r in storage.conn.execute("SELECT DISTINCT stage FROM audit_log").fetchall()
        }
        assert {"m1_extract", "m4_chunk", "m5_gate", "m6_map", "m7_verify", "m8_reconcile"} <= stages

    def test_reconcile_ladder_survives_garbage_model_output(self, sg_run):
        # the fake returns non-JSON for group prompts: exactly one controlling
        # record per (economy, indicator) group must still emerge (RULE wins)
        storage, report = sg_run
        assert report.n_groups > 0
        passed = storage.load_mappings(economy="SG", verification_status="passed")
        by_group: dict[str, list[MappingRecord]] = {}
        for r in passed:
            by_group.setdefault(r.indicator_id, []).append(r)
        for ind, members in by_group.items():
            assert sum(1 for m in members if m.controlling_evidence) == 1, ind

    def test_gate_scores_persisted_for_confidence(self, sg_run):
        storage, _ = sg_run
        scores = storage.load_gate_scores()
        assert scores
        assert all(0.0 <= v <= 1.0 for v in scores.values())


def seed_db_from_goldens(db_path: Path) -> Storage:
    """golden m1 stream + m4 offsets + m8 records + m5 cosines into a fresh DB."""
    slug = "sg_telecommunications_act_1999"
    doc_id = f"doc_{slug}"
    storage = Storage(db_path)
    storage.apply_schema()
    with gzip.open(ROOT / f"tests/golden/m1/{slug}.json.gz", "rt", encoding="utf-8") as f:
        canonical = json.load(f)
    storage.upsert_document(
        doc_id, "SG", "f" * 64, full_text=canonical["full_text"],
        extractor=canonical["extractor"], extractor_version=canonical["extractor_version"],
    )
    with gzip.open(ROOT / f"tests/golden/m4/{slug}.chunks.json.gz", "rt", encoding="utf-8") as f:
        chunks = json.load(f)["chunks"]
    storage.conn.executemany(
        "INSERT INTO chunks (chunk_id, document_id, char_start, char_end,"
        " section_label, chunk_kind, created_at) VALUES (?, ?, ?, ?, ?, ?, '')",
        [
            (c["chunk_id"], doc_id, c["char_start"], c["char_end"],
             c["section_label"], c["chunk_kind"])
            for c in chunks
        ],
    )
    storage.conn.commit()
    with gzip.open(ROOT / f"tests/golden/m8/{slug}.reconciled.json.gz", "rt", encoding="utf-8") as f:
        records = [MappingRecord.model_validate(r) for r in json.load(f)["records"]]
    storage.upsert_mappings(records, run_id="run_one")
    with gzip.open(ROOT / f"tests/golden/m5/{slug}.gated.json.gz", "rt", encoding="utf-8") as f:
        gated = json.load(f)
    storage.upsert_gate_scores({
        (r["chunk"]["chunk_id"], r["indicator_id"]): r["cosine_pillar"]
        for r in gated["passed"]
    })
    return storage


class TestExportFromDb:
    def test_export_over_golden_records(self, tmp_path):
        """M9 from the database alone: the round-trip through storage keeps
        the battery green."""
        storage = seed_db_from_goldens(tmp_path / "db.sqlite")
        result = export_from_db(storage, tmp_path / "out")
        assert result.battery_failures == []
        assert result.csv_path.exists()
        assert len(result.rows) > 0
        stages = {
            r["stage"]
            for r in storage.conn.execute("SELECT DISTINCT stage FROM audit_log").fetchall()
        }
        assert "m9_export" in stages

    def test_empty_db_refuses_readably(self, tmp_path):
        storage = Storage(tmp_path / "db.sqlite")
        storage.apply_schema()
        with pytest.raises(RuntimeError, match="regcompass run"):
            export_from_db(storage, tmp_path / "out")

    def test_label_repair_runs_before_the_pointer_gate(self, tmp_path):
        """The live lane must repair labels with the same
        rule set as the repro lane BEFORE the fail-closed pointer gate judges
        them. A record whose section number contradicts its quote's actual
        heading is repaired at export, not hard-stopped."""
        storage = seed_db_from_goldens(tmp_path / "db.sqlite")
        rec = storage.load_mappings()[0]
        storage.upsert_mappings(
            [rec.model_copy(update={"section": "s. 9999"})], run_id="run_one"
        )
        result = export_from_db(storage, tmp_path / "out")
        assert result.battery_failures == []
        row = storage.conn.execute(
            "SELECT decision FROM audit_log WHERE stage = 'm9_label_repair'"
        ).fetchone()
        assert row is not None
        assert "repaired=" in row["decision"]
        n = int(row["decision"].split("repaired=")[1].split(" ")[0])
        assert n >= 1

    def test_label_repair_is_idempotent_on_clean_goldens(self, tmp_path):
        """Golden m8 labels already agree with their quotes: the repair pass
        must keep them verbatim (repaired=0) and the export must stay
        byte-identical to a repair-free battery-green export."""
        storage = seed_db_from_goldens(tmp_path / "db.sqlite")
        result = export_from_db(storage, tmp_path / "out")
        assert result.battery_failures == []
        row = storage.conn.execute(
            "SELECT decision FROM audit_log WHERE stage = 'm9_label_repair'"
        ).fetchone()
        assert row["decision"].startswith("repaired=0 ")


class TestCli:
    """The judge-facing command surface (typer runner, all offline)."""

    def _runner(self):
        from typer.testing import CliRunner

        from regcompass.cli import app

        return CliRunner(), app

    def test_run_rejects_unknown_economy(self):
        runner, app = self._runner()
        r = runner.invoke(app, ["run", "--economy", "XX"])
        assert r.exit_code == 2

    def test_run_rejects_unknown_engine(self):
        runner, app = self._runner()
        r = runner.invoke(app, ["run", "--economy", "SG", "--engine", "cloud"])
        assert r.exit_code == 2

    def test_export_without_db_refuses_readably(self, tmp_path):
        runner, app = self._runner()
        r = runner.invoke(app, ["export", "--db", str(tmp_path / "missing.db")])
        assert r.exit_code == 2
        assert "regcompass run" in r.output

    def test_export_happy_path_from_seeded_db(self, tmp_path, monkeypatch):
        monkeypatch.chdir(ROOT)  # config/ resolution
        db = tmp_path / "db.sqlite"
        seed_db_from_goldens(db)
        runner, app = self._runner()
        r = runner.invoke(app, ["export", "--db", str(db), "--out", str(tmp_path / "out")])
        assert r.exit_code == 0, r.output
        assert "battery GREEN" in r.output
        assert (tmp_path / "out" / "submission.csv").exists()

    def test_audit_log_command_reads_the_filled_trail(self, tmp_path, monkeypatch):
        monkeypatch.chdir(ROOT)
        db = tmp_path / "db.sqlite"
        storage = seed_db_from_goldens(db)
        export_from_db(storage, tmp_path / "out")
        runner, app = self._runner()
        r = runner.invoke(app, ["audit-log", "--db", str(db)])
        assert r.exit_code == 0
        assert "m9_export" in r.output


class TestTransportRetry:
    """A timeout is a TRANSPORT failure (no model output): the ladder retries
    it without touching the 3-attempt content budget; persistent failure
    raises loudly instead of shipping a partial run."""

    def _quiet_sleep(self, monkeypatch):
        import time

        monkeypatch.setattr(time, "sleep", lambda s: None)

    def test_recovers_after_transient_errors(self, monkeypatch):
        from regcompass.pipeline import _with_transport_retry

        self._quiet_sleep(monkeypatch)
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise TimeoutError("connection timed out")
            return "ok"

        assert _with_transport_retry("m6 test", lambda s: None, flaky) == "ok"
        assert calls["n"] == 3

    def test_persistent_failure_raises_loudly(self, monkeypatch):
        from regcompass.pipeline import _with_transport_retry

        self._quiet_sleep(monkeypatch)

        def dead():
            raise ConnectionError("no route to host")

        with pytest.raises(RuntimeError, match="persisted after 3"):
            _with_transport_retry("m8 SG", lambda s: None, dead)


class TestConfigErrorFailFast:
    """A missing or rejected API key is a CONFIG error, not
    a transport blip. It must escape the retry ladder on the first call (no
    backoff burned) and fail the CLI fast and readably - never a green banner
    over zero records."""

    def test_ladder_reraises_config_error_immediately(self):
        from regcompass.map import ConfigError
        from regcompass.pipeline import _with_transport_retry

        calls = {"n": 0}

        def no_key():
            calls["n"] += 1
            raise ConfigError(
                "Engine 'engine-b' (Engine B) needs OPENROUTER_API_KEY (in .env); not set"
            )

        # time.sleep is NOT monkeypatched: a single raise proves no backoff.
        with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
            _with_transport_retry("m6 pair", lambda s: None, no_key, required=False)
        assert calls["n"] == 1

    def test_ladder_reraises_authentication_error_by_name(self):
        """litellm's 401 class, matched by name so the base tier never needs
        litellm importable."""
        from regcompass.pipeline import _with_transport_retry

        class AuthenticationError(Exception):
            pass

        def rejected():
            raise AuthenticationError("401: invalid API key")

        with pytest.raises(AuthenticationError):
            _with_transport_retry("m6 pair", lambda s: None, rejected, required=False)

    def test_cli_keyed_engine_without_key_fails_fast(self, tmp_path, monkeypatch):
        import time

        from typer.testing import CliRunner

        from regcompass.cli import app

        monkeypatch.chdir(tmp_path)  # no .env here; CONFIG_DIR is absolute
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        t0 = time.monotonic()
        r = CliRunner().invoke(
            app,
            ["run", "--economy", "SG", "--engine", "engine-b", "--db", str(tmp_path / "db.sqlite")],
        )
        elapsed = time.monotonic() - t0
        assert r.exit_code == 2
        assert "OPENROUTER_API_KEY (in .env, or typed into Settings); not set" in r.output
        assert elapsed < 5  # preflight, not 35s-per-pair backoff

    def test_cli_keyed_engine_with_key_passes_the_preflight(self, tmp_path, monkeypatch):
        """The preflight only checks presence; with a key set the command gets
        past the gate and reaches the Run, which here stops on the empty Corpus
        of a scratch database. That is a different refusal, in different words:
        the key gate is silent."""
        from typer.testing import CliRunner

        from regcompass.cli import app

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-not-a-real-key")
        r = CliRunner().invoke(
            app,
            ["run", "--economy", "SG", "--engine", "engine-b", "--db", str(tmp_path / "db.sqlite")],
        )
        assert "not set" not in r.output
        assert "no Documents in the Corpus for Singapore" in r.output


class TestAHungEngineCallCannotStallARun:
    """Twice on 23 Sep 2026 a paid Run stopped dead mid-Document: one request
    was still open at the provider, the library's own per-request timeout never
    fired, so the transport ladder never got its turn and the Mapping pool, which
    reads its results in pair order, waited on that one pair forever.

    With the wall-clock deadline RegCompass enforces itself, the same hang is
    an ordinary transport failure: the pair is skipped, the Run finishes, and
    the Run Record says in words which wall the pair hit."""

    def test_one_hung_pair_is_skipped_and_the_run_still_completes(
        self, tmp_path, monkeypatch
    ):
        import threading

        import regcompass.engines as engines_mod
        import regcompass.pipeline as pipeline_mod
        from regcompass.engines import call_deadline_seconds, call_with_deadline

        # No real backoff and no real deadline: the behaviour under test is the
        # wall, not the waiting.
        monkeypatch.setattr(pipeline_mod, "_sleep", lambda s: None)
        monkeypatch.setattr(engines_mod, "CALL_DEADLINE_SECONDS", 0.2)

        released = threading.Event()
        target: dict[str, str | None] = {"prompt": None}
        seen = {"n": 0}

        def never_answers() -> str:
            released.wait(30)
            return "too late"

        def completion(prompt: str, strict: bool) -> str:
            """The fake Engine, except that the second Mapping prompt of the Run
            is answered by a call that never returns, on every try."""
            if _INDICATOR_RE.search(prompt) and _PROVISION_RE.search(prompt):
                seen["n"] += 1
                if target["prompt"] is None and seen["n"] == 2:
                    target["prompt"] = prompt
            if target["prompt"] is not None and prompt == target["prompt"]:
                return call_with_deadline(
                    never_answers, call_deadline_seconds(180), "fake"
                )
            return fake_completion(prompt, strict)

        storage = Storage(tmp_path / "hung.db")
        storage.apply_schema()
        seed_corpus(storage, tmp_path / "data", "SG")
        try:
            report = run_economy(
                storage, "SG", pillars=(7,), engine=resolve_engine("fake"),
                completion_fn=completion, embed_fn=fake_embed,
                data_dir=tmp_path / "data",
            )
        finally:
            released.set()

        record = storage.run_get(report.run_id)
        assert record["status"] == "completed"
        assert record["error"] is None

        skipped = [n for n in record["details"]["notes"] if "deadline" in n]
        assert len(skipped) == 1, record["details"]["notes"]
        assert "transport failure persisted" in skipped[0]
        assert "EngineCallDeadline" in skipped[0]
        assert report.n_dropped >= 1

        # The other pairs are untouched: one dead pair costs one pair.
        rows = storage.conn.execute(
            "SELECT decision FROM audit_log WHERE stage = 'm6_map' ORDER BY id"
        ).fetchall()
        assert len(rows) > 1
        assert sum(1 for r in rows if "deadline" in r["decision"]) == 1
        assert any(r["decision"].startswith("mapped") for r in rows)
