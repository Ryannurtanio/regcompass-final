"""A Run owns its Mappings, word boxes live in the working database,
and a Run can be narrowed to a list of Indicators.

Before this, `mapping_id` was `chunk_id::indicator_id` and nothing else, so a
second Run over the same Economy silently overwrote the first, reconciliation
mixed both Engines' records, and the highlight geometry the audit view needs
existed only inside the frozen bundle. Every test here runs offline on the fake
Engine over the committed SG fixture Corpus.
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from regcompass.cli import app
from regcompass.config import UnknownIndicator, narrow_indicators
from regcompass.engines import fake_completion, fake_embed, resolve_engine
from regcompass.pipeline import export_from_db, run_economy
from regcompass.storage import LEGACY_RUN_ID, Storage

from corpus_fixtures import seed_corpus  # noqa: E402

PILLAR = (7,)


def _scripted_engine(drop_leading_words: int):
    """A fake Engine that answers the PASS lane with the LAST long line of the
    provision, optionally starting a few words in.

    Two Runs must produce genuinely different quotes to prove they are stored,
    reconciled and exported apart. Shifting the start inside one line rather
    than picking a different line keeps every record in the same section on the
    same page, so both Runs stay battery-green and the only difference between
    their exports is the text of the quotes."""

    def completion(prompt: str, strict: bool) -> str:
        import json

        from regcompass.engines import _INDICATOR_RE, _PROVISION_RE, record_usage

        indicator = _INDICATOR_RE.search(prompt)
        provision = _PROVISION_RE.search(prompt)
        if indicator is None or provision is None:
            return fake_completion(prompt, strict)
        # 7.1 and 7.3 drive the no-evidence and drop lanes; leave them alone.
        if indicator.group(1) in ("7.1", "7.3"):
            return fake_completion(prompt, strict)
        lines = [
            line.strip()
            for line in provision.group(1).splitlines()
            if len(line.strip()) >= 40
        ]
        if not lines:
            return fake_completion(prompt, strict)
        quote = lines[-1]
        if drop_leading_words:
            shortened = quote.split(" ", drop_leading_words)[-1]
            if len(shortened) >= 40:
                quote = shortened
        answer = json.dumps(
            {
                "maps_to_indicator": True,
                "verbatim_quote": quote,
                "impact": "The provision regulates the matter described in the quote.",
            }
        )
        # metered exactly as fake_completion does it, so the Run Record's token
        # totals stay honest on this lane too
        record_usage(len(prompt) // 4, len(answer) // 4)
        return answer

    return completion


@pytest.fixture(scope="module")
def two_runs(tmp_path_factory):
    """Two fake-Engine Runs over one seeded SG Corpus, answering with different
    verbatim quotes. Returns (storage, run_a, run_b)."""
    root = tmp_path_factory.mktemp("runscope")
    storage = Storage(root / "regcompass.db")
    storage.apply_schema()
    seed_corpus(storage, root / "data", "SG")
    a = run_economy(
        storage, "SG", pillars=PILLAR, engine=resolve_engine("fake"),
        completion_fn=_scripted_engine(0), embed_fn=fake_embed, data_dir=root / "data",
    )
    b = run_economy(
        storage, "SG", pillars=PILLAR, engine=resolve_engine("fake"),
        completion_fn=_scripted_engine(2), embed_fn=fake_embed, data_dir=root / "data",
    )
    return storage, a, b


class TestTwoRunsCoexist:
    def test_two_run_records_completed(self, two_runs):
        storage, a, b = two_runs
        assert a.run_id != b.run_id
        records = storage.runs_list(economy="SG", kind="run")
        assert {r["run_id"] for r in records} >= {a.run_id, b.run_id}
        assert all(r["status"] == "completed" for r in records)

    def test_each_run_keeps_a_complete_mapping_set(self, two_runs):
        storage, a, b = two_runs
        first = storage.load_mappings(economy="SG", run_id=a.run_id)
        second = storage.load_mappings(economy="SG", run_id=b.run_id)
        assert first and second
        # The second Run no longer overwrites the first: both sets survive and
        # cover the same (chunk, indicator) pairs.
        assert {r.mapping_id for r in first} == {r.mapping_id for r in second}
        assert len(storage.load_mappings(economy="SG")) == len(first) + len(second)

    def test_mapping_id_string_format_is_unchanged(self, two_runs):
        storage, a, _ = two_runs
        for rec in storage.load_mappings(economy="SG", run_id=a.run_id):
            assert rec.mapping_id == f"{rec.chunk_id}::{rec.indicator_id}"

    def test_the_two_runs_produced_different_quotes(self, two_runs):
        storage, a, b = two_runs
        first = {r.mapping_id: r.verbatim_quote for r in
                 storage.load_mappings(economy="SG", run_id=a.run_id, verification_status="passed")}
        second = {r.mapping_id: r.verbatim_quote for r in
                  storage.load_mappings(economy="SG", run_id=b.run_id, verification_status="passed")}
        shared = set(first) & set(second)
        assert shared, "the two Runs must share mapping ids"
        assert any(first[k] != second[k] for k in shared)


class TestReconcileScopedToOneRun:
    def test_each_run_has_its_own_controlling_record_per_indicator(self, two_runs):
        storage, a, b = two_runs
        for run_id in (a.run_id, b.run_id):
            passed = storage.load_mappings(
                economy="SG", run_id=run_id, verification_status="passed"
            )
            groups: dict[str, list] = {}
            for rec in passed:
                groups.setdefault(rec.indicator_id, []).append(rec)
            assert groups
            for indicator, members in groups.items():
                controlling = [m for m in members if m.controlling_evidence]
                assert len(controlling) == 1, (run_id, indicator, len(controlling))

    def test_reconcile_named_its_run_in_the_audit_log(self, two_runs):
        storage, a, b = two_runs
        decisions = [
            r["decision"]
            for r in storage.conn.execute(
                "SELECT decision FROM audit_log WHERE stage = 'm8_reconcile'"
            )
        ]
        assert any(a.run_id in d for d in decisions)
        assert any(b.run_id in d for d in decisions)


class TestExportScopedToOneRun:
    def _rows(self, path: Path) -> list[dict]:
        with path.open(encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))

    def test_each_run_exports_its_own_records(self, two_runs, tmp_path):
        storage, a, b = two_runs
        first = export_from_db(storage, tmp_path / "a", run_id=a.run_id)
        second = export_from_db(storage, tmp_path / "b", run_id=b.run_id)
        rows_a, rows_b = self._rows(first.csv_path), self._rows(second.csv_path)
        assert rows_a and rows_b
        assert rows_a != rows_b

    def test_export_without_a_run_id_picks_the_latest_completed_run(self, two_runs, tmp_path):
        storage, _, b = two_runs
        latest = export_from_db(storage, tmp_path / "latest")
        explicit = export_from_db(storage, tmp_path / "explicit", run_id=b.run_id)
        assert self._rows(latest.csv_path) == self._rows(explicit.csv_path)

    def test_export_refuses_when_no_run_has_completed(self, tmp_path):
        """A database that files Run Records but has finished none must not
        quietly export every row under a line claiming it has no Run Records."""
        from regcompass.pipeline import NoCompletedRunError
        from regcompass.storage import utc_now_z

        storage = Storage(tmp_path / "unfinished.db")
        storage.apply_schema()
        storage.run_start(
            run_id="run_running", kind="run", economy="SG", pillars=[7],
            indicators=None, engine="fake", started_at=utc_now_z(),
        )
        storage.run_start(
            run_id="run_broken", kind="run", economy="SG", pillars=[7],
            indicators=None, engine="fake", started_at=utc_now_z(),
        )
        storage.run_finish(
            "run_broken", status="failed", ended_at=utc_now_z(), error="boom"
        )
        with pytest.raises(NoCompletedRunError, match="--run-id"):
            export_from_db(storage, tmp_path / "out")
        storage.close()

        result = CliRunner().invoke(
            app,
            ["export", "--db", str(tmp_path / "unfinished.db"),
             "--out", str(tmp_path / "cliout")],
        )
        assert result.exit_code == 2, result.output
        assert "no completed Run" in result.output

    def test_a_completed_run_with_no_mappings_says_so(self, tmp_path):
        """A Run that finished cleanly and mapped nothing is a RESULT, not a
        forgotten step: telling the operator to `run regcompass run first` sent
        them round a loop they had just completed (paid smoke, 16 Sep 2026,
        Engine A on Singapore Pillar 6)."""
        from regcompass.storage import utc_now_z

        db = tmp_path / "empty_run.db"
        storage = Storage(db)
        storage.apply_schema()
        storage.run_start(
            run_id="run_nothing", kind="run", economy="SG", pillars=[6],
            indicators=None, engine="engine-a", started_at=utc_now_z(),
        )
        storage.run_finish("run_nothing", status="completed", ended_at=utc_now_z())
        with pytest.raises(RuntimeError, match="completed with no Mappings") as e:
            export_from_db(storage, tmp_path / "out")
        assert "run_nothing" in str(e.value)
        assert "regcompass run` first" not in str(e.value)
        storage.close()

        result = CliRunner().invoke(
            app, ["export", "--db", str(db), "--out", str(tmp_path / "cliout")]
        )
        assert result.exit_code == 1, result.output
        assert "completed with no Mappings" in result.output

    def test_a_run_whose_mappings_all_failed_verification_says_so(self, tmp_path):
        """The third silence: Mappings exist, none of them passed the
        byte-for-byte check. That is neither a forgotten Run nor an Engine that
        found nothing, and the operator needs to know which one it was. Its own
        database, because it rewrites every verification_status in it."""
        db = tmp_path / "unverified.db"
        storage = Storage(db)
        storage.apply_schema()
        seed_corpus(storage, tmp_path / "data", "SG")
        run = run_economy(
            storage, "SG", pillars=PILLAR, engine=resolve_engine("fake"),
            completion_fn=_scripted_engine(0), embed_fn=fake_embed,
            data_dir=tmp_path / "data",
        )
        storage.conn.execute(
            "UPDATE mappings SET verification_status = 'dropped' WHERE run_id = ?",
            (run.run_id,),
        )
        storage.conn.commit()
        assert storage.load_mappings(run_id=run.run_id)
        with pytest.raises(RuntimeError, match="none passed verification") as e:
            export_from_db(storage, tmp_path / "out", run_id=run.run_id)
        assert run.run_id in str(e.value)
        assert "no Mappings" not in str(e.value)
        storage.close()

        result = CliRunner().invoke(
            app,
            ["export", "--db", str(db), "--out", str(tmp_path / "cliout"),
             "--run-id", run.run_id],
        )
        assert result.exit_code == 1, result.output
        assert "none passed verification" in result.output

    def test_a_database_with_no_run_records_still_exports_every_row(self, tmp_path):
        """The legacy fallback stays: a golden seeded straight from checkpoints
        files no Run Record and must export exactly as it always did."""
        from test_pipeline import seed_db_from_goldens

        storage = seed_db_from_goldens(tmp_path / "golden.db")
        assert storage.runs_list(limit=1) == []
        result = export_from_db(storage, tmp_path / "out")
        assert result.battery_failures == []
        assert result.rows
        storage.close()

    def test_export_names_the_run_it_shipped_in_the_audit_log(self, two_runs, tmp_path):
        storage, a, _ = two_runs
        export_from_db(storage, tmp_path / "named", run_id=a.run_id)
        decisions = [
            r["decision"]
            for r in storage.conn.execute(
                "SELECT decision FROM audit_log WHERE stage = 'm9_export'"
            )
        ]
        assert any(a.run_id in d for d in decisions)


class TestWordBoxesInTheWorkingDatabase:
    def test_ingest_wrote_word_rows(self, two_runs):
        storage, _, _ = two_runs
        n = storage.conn.execute("SELECT COUNT(*) AS n FROM document_words").fetchone()["n"]
        assert n > 1000

    def test_words_are_sliced_back_byte_faithfully(self, two_runs):
        storage, _, _ = two_runs
        doc_id = storage.conn.execute(
            "SELECT document_id FROM documents LIMIT 1"
        ).fetchone()["document_id"]
        full_text = storage.document_full_text(doc_id)
        words = storage.words_for(doc_id)[:200]
        assert words
        for w in words:
            assert full_text[w.char_start:w.char_end] == w.text

    def test_a_runs_mapping_resolves_to_page_and_boxes(self, two_runs):
        from regcompass.audit import DatabaseAuditSource

        storage, a, _ = two_runs
        source = DatabaseAuditSource(storage, run_id=a.run_id)
        passed = storage.load_mappings(
            economy="SG", run_id=a.run_id, verification_status="passed"
        )
        assert passed
        found = 0
        for rec in passed[:5]:
            detail = source.record_detail(rec.mapping_id, {})
            assert detail is not None
            if detail.highlight_available:
                found += 1
                assert detail.quote_char_start is not None
                for rect in detail.highlights:
                    assert rect.page >= 1
                    assert rect.x1 > rect.x0 and rect.y1 > rect.y0
        assert found, "a verified quote must highlight from the working database"

    def test_unknown_mapping_id_reads_as_missing(self, two_runs):
        from regcompass.audit import DatabaseAuditSource

        storage, a, _ = two_runs
        source = DatabaseAuditSource(storage, run_id=a.run_id)
        assert source.record_detail("doc_nope:c0001::7.1", {}) is None


class TestIndicatorNarrowing:
    def test_narrow_indicators_checks_membership(self):
        assert narrow_indicators(None, pillars=PILLAR) is None
        assert narrow_indicators([], pillars=PILLAR) is None
        assert narrow_indicators(["7.3", "7.1"], pillars=PILLAR) == ("7.1", "7.3")
        with pytest.raises(UnknownIndicator) as excinfo:
            narrow_indicators(["6.1"], pillars=PILLAR)
        assert "6.1" in str(excinfo.value) and "7.1" in str(excinfo.value)

    def test_a_narrowed_run_gates_only_those_indicators(self, tmp_path, monkeypatch):
        """The live test is one Pillar and two Indicators, driven from the
        command line, so the flag is exercised end to end rather than the
        function it calls."""
        monkeypatch.chdir(Path(__file__).resolve().parents[1])  # config/ resolution
        db = tmp_path / "narrow.db"
        storage = Storage(db)
        storage.apply_schema()
        seed_corpus(storage, tmp_path / "data", "SG")
        storage.close()

        result = CliRunner().invoke(
            app,
            ["run", "--economy", "SG", "--pillar", "7", "--engine", "fake",
             "--indicator", "7.1", "--indicator", "7.3",
             "--db", str(db), "--data-dir", str(tmp_path / "data")],
        )
        assert result.exit_code == 0, result.output

        storage = Storage(db)
        gated = {
            r["indicator_id"]
            for r in storage.conn.execute("SELECT DISTINCT indicator_id FROM gate_scores")
        }
        assert gated == {"7.1", "7.3"}
        run_id = storage.latest_run_id("SG")
        mapped = {
            r.indicator_id for r in storage.load_mappings(economy="SG", run_id=run_id)
        }
        assert mapped <= {"7.1", "7.3"}
        assert storage.run_get(run_id)["indicators"] == ["7.1", "7.3"]
        storage.close()

    def test_an_unnarrowed_run_still_covers_the_whole_pillar(self, two_runs):
        storage, a, _ = two_runs
        assert storage.run_get(a.run_id)["indicators"] is None
        mapped = {
            r.indicator_id for r in storage.load_mappings(economy="SG", run_id=a.run_id)
        }
        assert len(mapped) > 2

    def test_cli_run_refuses_an_indicator_outside_the_pillar(self, tmp_path):
        result = CliRunner().invoke(
            app,
            ["run", "--economy", "SG", "--pillar", "7", "--engine", "fake",
             "--indicator", "6.1", "--db", str(tmp_path / "x.db")],
        )
        assert result.exit_code == 2, result.output
        assert "6.1" in result.output and "7.1" in result.output

    def test_server_refuses_an_indicator_outside_the_pillar(self, tmp_path):
        from regcompass.server import create_app

        client = TestClient(create_app(tmp_path / "s.db", tmp_path / "out", tmp_path / "data"))
        resp = client.post(
            "/api/run",
            json={"economy": "SG", "pillars": [7], "engine": "fake",
                  "indicators": ["6.1"]},
        )
        assert resp.status_code == 400
        assert "6.1" in resp.text and "7.1" in resp.text


class TestTablesThatPointAtAMapping:
    """reviews, source_groups and mapping_relationships all reference a Mapping.
    A single-column `REFERENCES mappings(mapping_id)` stops matching the moment
    the primary key becomes composite, and SQLite answers "foreign key mismatch"
    on every INSERT rather than at schema time. Nothing writes these tables from
    the database lane yet (the review methods come later), so this is the
    only thing standing between those methods and a table they cannot write to.
    """

    @pytest.fixture()
    def one_run(self, tmp_path):
        """A database holding one Run with one Mapping, foreign keys enforced."""
        from regcompass.contracts import MappingRecord
        from regcompass.storage import utc_now_z

        storage = Storage(tmp_path / "fk.db")
        storage.apply_schema()
        storage.upsert_document("doc_sg_a", "SG", "a" * 64, full_text="some text")
        storage.conn.execute(
            "INSERT INTO chunks (chunk_id, document_id, char_start, char_end,"
            " section_label, created_at) VALUES ('doc_sg_a:c0001', 'doc_sg_a',"
            " 0, 9, 's. 1', 'now')"
        )
        storage.conn.commit()
        storage.run_start(
            run_id="run_alpha", kind="run", economy="SG", pillars=[7],
            indicators=None, engine="fake", started_at=utc_now_z(),
        )
        storage.upsert_mappings(
            [MappingRecord(
                mapping_id="doc_sg_a:c0001::7.1", document_id="doc_sg_a",
                chunk_id="doc_sg_a:c0001", economy="SG", indicator_id="7.1",
                indicator_name="An indicator", section="s. 1",
                verbatim_quote="some text", verification_status="passed",
            )],
            run_id="run_alpha",
        )
        yield storage
        storage.close()

    def test_foreign_keys_are_actually_enforced(self, one_run):
        """Without this the three tests below would pass vacuously."""
        assert one_run.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def test_a_review_of_a_runs_mapping_inserts(self, one_run):
        one_run.conn.execute(
            "INSERT INTO reviews (review_id, run_id, mapping_id, review_status,"
            " reviewer, reviewed_at, comment) VALUES ('rev_1', 'run_alpha',"
            " 'doc_sg_a:c0001::7.1', 'accepted', 'a reviewer', 'now', NULL)"
        )
        one_run.conn.commit()
        row = one_run.conn.execute("SELECT * FROM reviews").fetchone()
        assert (row["run_id"], row["mapping_id"], row["review_status"]) == (
            "run_alpha", "doc_sg_a:c0001::7.1", "accepted"
        )

    def test_a_review_naming_the_wrong_run_is_refused(self, one_run):
        """The composite key BINDS: the same mapping id under a Run that never
        produced it is not a Mapping, and the review is rejected rather than
        silently attached to the wrong Engine's record."""
        with pytest.raises(sqlite3.IntegrityError):
            one_run.conn.execute(
                "INSERT INTO reviews (review_id, run_id, mapping_id, review_status,"
                " reviewed_at) VALUES ('rev_2', 'run_beta', 'doc_sg_a:c0001::7.1',"
                " 'accepted', 'now')"
            )
            one_run.conn.commit()

    def test_a_source_group_and_its_relationship_insert(self, one_run):
        one_run.conn.execute(
            "INSERT INTO source_groups (group_id, run_id, economy, indicator_id,"
            " authoritative_mapping_id, created_at) VALUES ('grp_1', 'run_alpha',"
            " 'SG', '7.1', 'doc_sg_a:c0001::7.1', 'now')"
        )
        one_run.conn.execute(
            "INSERT INTO mapping_relationships (run_id, mapping_id, group_id,"
            " relationship) VALUES ('run_alpha', 'doc_sg_a:c0001::7.1', 'grp_1',"
            " 'sole_source')"
        )
        one_run.conn.commit()
        assert one_run.conn.execute(
            "SELECT COUNT(*) AS n FROM mapping_relationships"
        ).fetchone()["n"] == 1

    def test_two_runs_can_each_group_the_same_indicator(self, one_run):
        """The old UNIQUE (economy, indicator_id) would have made the second
        Run's reconciliation collide with the first Run's group."""
        from regcompass.contracts import MappingRecord

        one_run.upsert_mappings(
            [MappingRecord(
                mapping_id="doc_sg_a:c0001::7.1", document_id="doc_sg_a",
                chunk_id="doc_sg_a:c0001", economy="SG", indicator_id="7.1",
                indicator_name="An indicator", section="s. 1",
                verbatim_quote="some text", verification_status="passed",
            )],
            run_id="run_beta",
        )
        for group, run in (("grp_a", "run_alpha"), ("grp_b", "run_beta")):
            one_run.conn.execute(
                "INSERT INTO source_groups (group_id, run_id, economy, indicator_id,"
                " authoritative_mapping_id, created_at) VALUES (?, ?, 'SG', '7.1',"
                " 'doc_sg_a:c0001::7.1', 'now')",
                (group, run),
            )
        one_run.conn.commit()
        assert one_run.conn.execute(
            "SELECT COUNT(*) AS n FROM source_groups"
        ).fetchone()["n"] == 2


# The pre-Run-scoping shape of the tables this migration rebuilds, as schema.sql
# spelled them at 79124bb: mapping_id alone is the primary key.
OLD_SCHEMA = """
CREATE TABLE documents (
    document_id TEXT PRIMARY KEY, economy TEXT NOT NULL, source_sha256 TEXT NOT NULL UNIQUE,
    full_text TEXT, local_path TEXT, created_at TEXT NOT NULL
);
CREATE TABLE chunks (
    chunk_id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(document_id),
    char_start INTEGER NOT NULL, char_end INTEGER NOT NULL, section_label TEXT NOT NULL,
    chunk_kind TEXT NOT NULL DEFAULT 'section', created_at TEXT NOT NULL
);
CREATE TABLE mappings (
    mapping_id TEXT PRIMARY KEY,
    chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id),
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    economy TEXT NOT NULL, indicator_id TEXT NOT NULL, indicator_name TEXT NOT NULL,
    section TEXT NOT NULL, subsection TEXT, verbatim_quote TEXT NOT NULL,
    page_number INTEGER, impact TEXT, rdtii_score_contribution REAL,
    insufficient_evidence INTEGER NOT NULL DEFAULT 0, confidence REAL,
    verification_status TEXT NOT NULL DEFAULT 'unverified', discovery_tag TEXT,
    measure_type TEXT, timeline_adopted TEXT, timeline_entry_into_force TEXT,
    timeline_last_amended TEXT, timeline_repeal_status TEXT, source_archived_url TEXT,
    source_access_date TEXT, uncertainty_flags TEXT NOT NULL DEFAULT '[]',
    controlling_evidence INTEGER, novelty_scope TEXT, relationship_to_group TEXT,
    extraction_attempts INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
    CHECK (verification_status IN ('unverified', 'passed', 'dropped'))
);
CREATE INDEX idx_mappings_group_key ON mappings(economy, indicator_id);
CREATE INDEX idx_mappings_status ON mappings(verification_status);
CREATE TABLE source_groups (
    group_id TEXT PRIMARY KEY, economy TEXT NOT NULL, indicator_id TEXT NOT NULL,
    authoritative_mapping_id TEXT NOT NULL REFERENCES mappings(mapping_id),
    reconciliation_notes TEXT, created_at TEXT NOT NULL, UNIQUE (economy, indicator_id)
);
CREATE TABLE reviews (
    review_id TEXT PRIMARY KEY, mapping_id TEXT NOT NULL REFERENCES mappings(mapping_id),
    review_status TEXT NOT NULL, reviewer TEXT, reviewed_at TEXT NOT NULL, comment TEXT
);
"""


class TestMigrationOfAPreExistingDatabase:
    @pytest.fixture()
    def legacy_db(self, tmp_path) -> Path:
        path = tmp_path / "old.db"
        conn = sqlite3.connect(path)
        conn.executescript(OLD_SCHEMA)
        conn.execute(
            "INSERT INTO documents (document_id, economy, source_sha256, full_text,"
            " created_at) VALUES ('doc_sg_old', 'SG', 'a', 'text', 'then')"
        )
        conn.execute(
            "INSERT INTO chunks (chunk_id, document_id, char_start, char_end,"
            " section_label, created_at) VALUES ('doc_sg_old:c0001', 'doc_sg_old',"
            " 0, 4, 's. 1', 'then')"
        )
        conn.execute(
            "INSERT INTO mappings (mapping_id, chunk_id, document_id, economy,"
            " indicator_id, indicator_name, section, verbatim_quote,"
            " verification_status, created_at) VALUES"
            " ('doc_sg_old:c0001::7.1', 'doc_sg_old:c0001', 'doc_sg_old', 'SG',"
            " '7.1', 'Old', 's. 1', 'text', 'passed', 'then')"
        )
        conn.commit()
        conn.close()
        return path

    def test_old_rows_survive_as_legacy(self, legacy_db):
        storage = Storage(legacy_db)
        storage.apply_schema()
        rows = storage.conn.execute("SELECT run_id, mapping_id FROM mappings").fetchall()
        assert [(r["run_id"], r["mapping_id"]) for r in rows] == [
            (LEGACY_RUN_ID, "doc_sg_old:c0001::7.1")
        ]
        assert storage.load_mappings(run_id=LEGACY_RUN_ID)
        storage.close()

    def test_the_rebuilt_table_takes_two_runs(self, legacy_db):
        from regcompass.contracts import MappingRecord

        storage = Storage(legacy_db)
        storage.apply_schema()
        record = MappingRecord(
            mapping_id="doc_sg_old:c0001::7.1", document_id="doc_sg_old",
            chunk_id="doc_sg_old:c0001", economy="SG", indicator_id="7.1",
            indicator_name="New", section="s. 1", verbatim_quote="text",
            verification_status="passed",
        )
        storage.upsert_mappings([record], run_id="run_one")
        storage.upsert_mappings([record], run_id="run_two")
        assert len(storage.load_mappings()) == 3
        assert len(storage.load_mappings(run_id="run_one")) == 1
        storage.close()

    def test_the_indexes_survived_the_rebuild(self, legacy_db):
        storage = Storage(legacy_db)
        storage.apply_schema()
        names = {
            r["name"]
            for r in storage.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
                " AND tbl_name = 'mappings'"
            )
        }
        assert {"idx_mappings_group_key", "idx_mappings_status"} <= names
        storage.close()

    def test_a_migrated_database_takes_a_review(self, legacy_db):
        """The rebuild has to reach reviews, source_groups and
        mapping_relationships too. If it left any of them pointing at
        mappings(mapping_id) alone, the table would open fine and refuse every
        insert with "foreign key mismatch" long after the migration."""
        storage = Storage(legacy_db)
        storage.apply_schema()
        assert storage.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        storage.conn.execute(
            "INSERT INTO reviews (review_id, run_id, mapping_id, review_status,"
            " reviewed_at) VALUES ('rev_legacy', ?, 'doc_sg_old:c0001::7.1',"
            " 'accepted', 'now')",
            (LEGACY_RUN_ID,),
        )
        storage.conn.execute(
            "INSERT INTO source_groups (group_id, run_id, economy, indicator_id,"
            " authoritative_mapping_id, created_at) VALUES ('grp_legacy', ?, 'SG',"
            " '7.1', 'doc_sg_old:c0001::7.1', 'now')",
            (LEGACY_RUN_ID,),
        )
        storage.conn.commit()
        assert storage.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        storage.close()

    def test_a_fresh_database_needs_no_rebuild(self, tmp_path):
        storage = Storage(tmp_path / "fresh.db")
        storage.apply_schema()
        storage.apply_schema()  # idempotent
        columns = {
            r["name"] for r in storage.conn.execute("PRAGMA table_info(mappings)")
        }
        assert "run_id" in columns
        assert "document_words" in storage.table_names()
        storage.close()
