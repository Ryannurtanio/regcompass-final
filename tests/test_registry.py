"""Economies and Indicators are DATA.

The promise this file guards: a reviewer can pick any Economy in the Portal
configuration and any of the 12 Pillars, and the pipeline runs, with no code
change anywhere. So the tests here drive the doors a user actually uses - the
command line, the server, the export - and assert on what the user would see.
Nothing here needs the organizer's xlsx (the committed extract stands in for
it), a network connection, or a model with a price.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

import regcompass.cli as cli_mod
from regcompass.classify import SCORED_INDICATORS, has_scoring_model
from regcompass.config import (
    CONFIG_DIR,
    ConfigInvalid,
    indicator_ids,
    load_crosswalk,
    load_indicators,
    load_keywords,
    load_pillar_description,
    load_pillars,
    load_portals,
    validate_config,
)
from regcompass.contracts import ORGANIZER_LANGUAGES, STRATEGY_NAMES, CorpusDoc

ROOT = Path(__file__).resolve().parents[1]


def _seed_sg_corpus(db: Path, data_dir: Path) -> None:
    """A Run reads the Corpus, so a CLI Run test needs one. Built the way
    Discovery builds it (tests/corpus_fixtures.py)."""
    from regcompass.storage import Storage

    from corpus_fixtures import seed_corpus

    storage = Storage(db)
    storage.apply_schema()
    seed_corpus(storage, data_dir, "SG")
    storage.close()
FIXTURES = Path(__file__).parent / "fixtures"
ORGANIZER_EXTRACT = FIXTURES / "organizer_indicators.json"

# The Round 1 Indicators. They are the ones whose hand-written name and
# definition, and whose scoring rubric, must survive intact.
ROUND1_INDICATORS = {"6.1", "6.2", "6.3", "6.4", "7.1", "7.2", "7.3", "7.4", "7.5"}


@pytest.fixture()
def config_copy(tmp_path):
    """A writable copy of config/, so a test can break one file without
    touching the committed configuration."""
    dst = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, dst)
    return dst


# ---------------------------------------------------------------------------
# 1. the generated Indicator file matches the organizer's sheets
# ---------------------------------------------------------------------------


class TestGeneratedIndicators:
    def test_id_set_and_count_match_the_organizer_sheets(self):
        """The committed extract is written by scripts/make_indicators.py from
        the two organizer sheets, so this runs without openpyxl or the xlsx."""
        extract = json.loads(ORGANIZER_EXTRACT.read_text(encoding="utf-8"))
        loaded = load_indicators()
        assert extract["reference_count"] == 62
        assert extract["methodology_count"] == 61
        assert list(loaded) == extract["reference_ids"]
        assert len(loaded) == extract["reference_count"]

    def test_the_awkward_ids_round_trip_as_text(self):
        """Three-level and leading-zero ids are the whole reason the id is text.
        4.1 is the Indicator Reference spelling; the Methodology sheet renders
        the same Indicator as 4.10 through a float number format, and we follow
        the sheet the judges read."""
        loaded = load_indicators()
        for text_id in ("4.01", "4.1", "12.01", "12.4.1", "12.4.7"):
            assert text_id in loaded, text_id
        assert "4.10" not in loaded
        assert loaded["4.01"].name != loaded["4.1"].name

    def test_pillar_12_three_level_ids_are_all_present(self):
        assert {f"12.4.{n}" for n in range(1, 8)} <= set(load_indicators())

    def test_every_pillar_is_covered_and_agrees_with_the_indicator_file(self):
        indicators = load_indicators()
        pillars = load_pillars()
        assert set(pillars) == set(range(1, 13))
        listed = [i for p in pillars.values() for i in p.indicator_ids]
        assert sorted(listed) == sorted(indicators)
        for number, pillar in pillars.items():
            assert all(indicators[i].pillar == number for i in pillar.indicator_ids)

    def test_the_round1_definitions_are_kept_verbatim(self):
        """The golden evidence files pin the Round 1 names and definitions, so
        the generator must never rewrite them."""
        loaded = load_indicators()
        for key in ROUND1_INDICATORS:
            assert loaded[key].definition_derived is False
        assert loaded["6.1"].name == "Ban and local processing requirements"
        assert loaded["7.4"].name == (
            "Data Protection Impact Assessment or Data Protection Officer requirements"
        )

    def test_the_new_definitions_are_marked_derived_and_carry_the_criteria(self):
        loaded = load_indicators()
        derived = {k: v for k, v in loaded.items() if v.definition_derived}
        assert len(derived) == len(loaded) - len(ROUND1_INDICATORS)
        for key, entry in derived.items():
            assert entry.definition.strip()
            if entry.criteria:
                assert "Scoring criteria:" in entry.definition

    def test_6_5_is_present_but_not_answerable_from_legislation(self):
        loaded = load_indicators()
        assert loaded["6.5"].legislation_mapped is False
        assert all(
            d.legislation_mapped for k, d in loaded.items() if k != "6.5"
        )
        assert "6.5" not in indicator_ids()
        assert "6.5" not in load_crosswalk().schemes["numeric"]

    def test_pillar_6_and_7_weights_are_the_organizers_own_values(self):
        """The organizer's Pillar 6 weights sum to 101 percent. We record the
        values as given and never assert that they sum to one."""
        loaded = load_indicators()
        assert loaded["6.1"].weight == 0.38
        assert loaded["6.5"].weight == 0.08
        assert loaded["7.4"].weight == 0.06
        assert all(loaded[i].weight is None for i in ("1.4", "12.9"))


# ---------------------------------------------------------------------------
# 2. config validation fails loudly on a gap
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_the_committed_configuration_is_green(self):
        assert validate_config() == []

    def test_a_missing_keyword_file_is_reported(self, config_copy):
        (config_copy / "pillar_9_keywords.json").unlink()
        problems = validate_config(config_copy)
        assert any("pillar 9" in p and "keyword file" in p for p in problems)

    def test_a_keyword_file_without_a_description_is_reported(self, config_copy):
        path = config_copy / "pillar_11_keywords.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        del raw["_pillar_description"]
        path.write_text(json.dumps(raw), encoding="utf-8")
        problems = validate_config(config_copy)
        assert any("_pillar_description" in p for p in problems)
        with pytest.raises(ConfigInvalid, match="_pillar_description"):
            load_pillar_description(11, config_copy)

    def test_an_indicator_without_a_definition_is_reported(self, config_copy):
        path = config_copy / "indicators.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["8.3"] = {**raw["8.3"], "definition": "   "}
        path.write_text(json.dumps(raw), encoding="utf-8")
        assert any("8.3" in p for p in validate_config(config_copy))

    def test_an_indicator_with_no_vocabulary_is_reported(self, config_copy):
        path = config_copy / "pillar_10_keywords.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        del raw["10.3"]
        path.write_text(json.dumps(raw), encoding="utf-8")
        assert any("10.3" in p for p in validate_config(config_copy))

    def test_check_config_reports_a_problem_and_exits_nonzero(self, monkeypatch):
        monkeypatch.setattr(
            "regcompass.config.validate_config", lambda *a, **k: ["pillar 9 is broken"]
        )
        r = CliRunner().invoke(cli_mod.app, ["check-config"])
        assert r.exit_code == 1
        assert "pillar 9 is broken" in r.output
        assert "INVALID" in r.output

    def test_check_config_is_green_on_the_committed_configuration(self):
        r = CliRunner().invoke(cli_mod.app, ["check-config"])
        assert r.exit_code == 0, r.output
        assert "config OK" in r.output
        assert "62 across 12 pillars" in r.output


# ---------------------------------------------------------------------------
# 3. a Run on a Pillar with no scoring rubric completes end to end
# ---------------------------------------------------------------------------


class TestPillarTwelveRun:
    """`run --engine fake --pillar 12` is the acceptance criterion: a Pillar the
    RDTII 2.1 scoring rubric never covered must map, verify and persist its
    Mappings, and simply derive no score. Offline: the fake Engine answers from
    the prompt and the gate embeds without Ollama."""

    def test_pillar_12_run_completes_and_persists_mappings(
        self, tmp_path, monkeypatch, no_ollama_gate
    ):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setattr(cli_mod, "_load_dotenv", lambda *a, **k: None)
        monkeypatch.chdir(ROOT)  # config/ resolution
        db = tmp_path / "db.sqlite"
        data = tmp_path / "data"
        _seed_sg_corpus(db, data)
        r = CliRunner().invoke(
            cli_mod.app,
            ["run", "--economy", "SG", "--pillar", "12", "--engine", "fake",
             "--db", str(db), "--data-dir", str(data)],
        )
        assert r.exit_code == 0, r.output
        assert "pillars=(12,)" in r.output

        from regcompass.storage import Storage

        records = Storage(db).load_mappings(economy="SG")
        assert records, "a pillar 12 run must persist mappings"
        assert {int(rec.indicator_id.split(".")[0]) for rec in records} == {12}
        assert any(rec.verification_status == "passed" for rec in records)
        for rec in records:
            assert rec.rdtii_score_contribution is None

    def test_a_pillar_12_indicator_has_no_scoring_model(self):
        assert SCORED_INDICATORS == ROUND1_INDICATORS
        assert has_scoring_model("6.1")
        assert not has_scoring_model("12.4.1")

    def test_classify_returns_the_mappings_only_basis_without_calling_a_model(self):
        from regcompass.classify import NO_SCORING_MODEL_BASIS, classify_record

        def never(*a, **k):
            raise AssertionError("the mappings-only path must call no model")

        record = _fake_record("12.4.1")
        out = classify_record(record, "chunk text", completion_fn=never, confirm_completion_fn=never)
        assert out.outcome == "no_scoring_model"
        assert out.classification is None
        assert out.basis == NO_SCORING_MODEL_BASIS

    def test_the_scored_tables_are_keyed_off_one_registry(self):
        """Every per-indicator scoring table must cover exactly the Indicators
        with a rubric, so a new Pillar can never half-land in one of them."""
        from regcompass.classify import ABSENCE_SCORE, ELIGIBLE_NATURES
        from regcompass.export import SCORE_IF_PRESENT, V2_ALLOWED_SCORES

        for table in (ELIGIBLE_NATURES, ABSENCE_SCORE, SCORE_IF_PRESENT, V2_ALLOWED_SCORES):
            assert set(table) == SCORED_INDICATORS


def _fake_record(indicator_id: str, economy: str = "SG"):
    from regcompass.contracts import MappingRecord

    return MappingRecord(
        mapping_id="map_test_0001",
        economy=economy,
        document_id=f"doc_{economy.lower()}_x",
        chunk_id=f"doc_{economy.lower()}_x:c0001",
        indicator_id=indicator_id,
        indicator_name="test indicator",
        section="s. 1",
        verbatim_quote="A quote long enough to look like a real statutory provision.",
        verification_status="passed",
    )


@pytest.fixture()
def no_ollama_gate(monkeypatch):
    """Any attempt to embed through Ollama is a test failure: the offline lane
    must carry the gate too."""
    import regcompass.gate as gate_mod

    def boom(*a, **k):
        raise AssertionError("the fake Engine must never reach Ollama")

    monkeypatch.setattr(gate_mod, "embed_ollama", boom)


# ---------------------------------------------------------------------------
# 4. adding an Economy is a configuration edit and nothing else
# ---------------------------------------------------------------------------


NEW_ECONOMY = """
  ZZ:
    official_name: Testland
    languages: [English]
    hosts: []
    strategy: manual
    prepared: false
    live_test_pool: false
    notes: A test-only Economy, added to prove no code edit is needed.
"""


@pytest.fixture()
def config_with_new_economy(config_copy, monkeypatch):
    """Append one Economy to a COPY of portals.yaml and point every loader at
    that copy. No source file is edited anywhere in this test."""
    path = config_copy / "portals.yaml"
    path.write_text(path.read_text(encoding="utf-8") + NEW_ECONOMY, encoding="utf-8")
    monkeypatch.setattr("regcompass.config.CONFIG_DIR", config_copy)
    return config_copy


class TestAddingAnEconomy:
    def test_it_appears_in_the_registry(self, config_with_new_economy):
        portals = load_portals(config_with_new_economy)
        assert "ZZ" in portals
        assert portals["ZZ"].official_name == "Testland"

    def test_it_appears_in_the_command_lines_choices(self, config_with_new_economy):
        assert "ZZ" in cli_mod.economies()
        r = CliRunner().invoke(cli_mod.app, ["run", "--economy", "QQ", "--engine", "fake"])
        assert r.exit_code == 2
        assert "ZZ" in r.output  # the choices the error lists come from the registry

    def test_the_server_offers_it_and_accepts_it(self, config_with_new_economy, tmp_path):
        from fastapi.testclient import TestClient

        from regcompass.server import create_app

        client = TestClient(
            create_app(
                db_path=str(tmp_path / "web.db"), out_dir=tmp_path,
                ui_dir=None,
            )
        )
        body = client.get("/api/status").json()
        assert "ZZ" in body["economies"]
        assert body["economy_names"]["ZZ"] == "Testland"
        # The new Economy is not merely listed: a Run on it passes the registry
        # check and reaches the Corpus question (409 corpus_empty names it by
        # its official name), where an unconfigured code is refused outright.
        # A Run over a real Corpus for a new Economy is tests/test_server.py.
        started = client.post("/api/run", json={"economy": "ZZ", "engine": "fake"})
        assert started.status_code == 409, started.text
        detail = started.json()["detail"]
        assert detail["economy"] == "ZZ"
        assert "Testland" in detail["message"]
        # An unknown code is still a 400, and the message lists the registry.
        bad = client.post("/api/run", json={"economy": "QQ", "engine": "fake"})
        assert bad.status_code == 400
        assert "ZZ" in bad.json()["detail"]


class TestEconomyRegistryShape:
    def test_every_economy_carries_the_coverage_matrix_spelling(self):
        portals = load_portals()
        assert portals["LA"].official_name == "Lao PDR"
        assert portals["VN"].official_name == "Viet Nam"
        assert portals["RU"].official_name == "Russian Federation"
        assert portals["LA"].un_name == "Lao People's Democratic Republic"
        assert portals["TL"].official_name == "Timor-Leste"

    def test_every_language_is_on_the_organizers_list(self):
        for code, portal in load_portals().items():
            assert portal.languages, code
            for language in portal.languages:
                assert language in ORGANIZER_LANGUAGES, (code, language)

    def test_every_live_test_economy_has_a_portal(self):
        pool = {"TH", "VN", "ID", "CN", "IN", "KZ", "LA", "MN", "RU", "TL"}
        portals = load_portals()
        assert pool <= set(portals)
        assert {c for c, p in portals.items() if p.live_test_pool} == pool

    def test_china_adds_by_url_from_the_regulator_and_never_from_the_law_database(self):
        """The national law database's robots.txt disallows every agent, so it
        is never whitelisted; the regulator's host republishes the same text
        and permits the law pages."""
        china = load_portals()["CN"]
        assert china.strategy == "manual"
        assert china.manual_only is False
        # The regulator stays the Portal host; the other official hosts the
        # baseline cites follow it.
        assert china.hosts[0] == "www.cac.gov.cn"
        assert "flk.npc.gov.cn" not in china.hosts

    def test_an_empty_host_whitelist_needs_the_manual_strategy(self):
        from pydantic import ValidationError

        from regcompass.contracts import PortalConfig

        with pytest.raises(ValidationError, match="manual"):
            PortalConfig(
                economy="ZZ",
                official_name="Testland",
                languages=["English"],
                hosts=[],
                strategy="httpx",
            )

    def test_a_language_off_the_organizers_list_is_refused(self):
        from pydantic import ValidationError

        from regcompass.contracts import PortalConfig

        with pytest.raises(ValidationError, match="organizer"):
            PortalConfig(
                economy="MY",
                official_name="Malaysia",
                languages=["Malay"],
                hosts=["lom.agc.gov.my"],
                strategy="httpx",
            )

    def test_the_strategy_registry_and_the_vocabulary_agree(self):
        from regcompass.crawl import STRATEGIES

        assert set(STRATEGIES) == set(STRATEGY_NAMES)
        for portal in load_portals().values():
            assert portal.strategy in STRATEGIES


# ---------------------------------------------------------------------------
# 5. the export writes the Coverage Matrix spelling and text ids
# ---------------------------------------------------------------------------


class TestExportSpellingAndTextIds:
    """Box 5 goes through the REAL writer: build_row assembles the row and the
    CSV writer puts it on disk, so the assertion is on what a judge opening the
    file would see, not on a dictionary we built ourselves."""

    def _row(self, indicator_id: str, economy: str):
        from regcompass.export import build_row

        portals = load_portals()
        doc = CorpusDoc(
            economy=economy,
            law_name="Law on Electronic Transactions",
            law_number_ref="No. 20/NA",
            last_amended="December 2023",
            source_url="https://example.gov/etl",
        )
        return build_row(
            _fake_record(indicator_id, economy=economy),
            doc,
            load_crosswalk(),
            {"database": {}, "inventory_law_keys": {}},
            {},
            {},
        ), portals[economy].official_name

    def test_the_writer_emits_text_ids_and_the_coverage_matrix_spelling(self, tmp_path):
        import csv

        from regcompass.export import COLUMNS, EXTRA_COLUMNS

        # Lao PDR and Viet Nam are the two-word Coverage Matrix spellings.
        cases = [("12.4.1", "LA"), ("4.01", "LA"), ("12.01", "VN"), ("6.1", "VN")]
        rows, names = [], []
        for indicator_id, economy in cases:
            row, official = self._row(indicator_id, economy)
            rows.append(row)
            names.append(official)
        assert names == ["Lao PDR", "Lao PDR", "Viet Nam", "Viet Nam"]

        header = list(COLUMNS) + list(EXTRA_COLUMNS)
        path = tmp_path / "submission.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            for row in rows:
                writer.writerow([row[col] for col in header])

        read_back = list(csv.DictReader(path.open(encoding="utf-8-sig")))
        assert [r["Indicator ID"] for r in read_back] == ["12.4.1", "4.01", "12.01", "6.1"]
        assert [r["Economy"] for r in read_back] == [
            "Lao PDR", "Lao PDR", "Viet Nam", "Viet Nam",
        ]

    def test_an_unscored_indicator_still_gets_a_full_evidence_row(self):
        row, _ = self._row("12.4.1", "LA")
        assert row["Verbatim Snippet"] == (
            "A quote long enough to look like a real statutory provision."
        )
        assert row["Source URL"] == "https://example.gov/etl"
        assert row["Article / Section"]


# ---------------------------------------------------------------------------
# 6. the old literals are gone from the shipped code
# ---------------------------------------------------------------------------


# Scoped to the product: src/, the interface source, and the served page.
# scripts/ and tests/golden/ legitimately keep the Round 1 literals, because
# they regenerate and pin FROZEN Round 1 artifacts.
#
# One file inside src/ is an intentional exclusion:
# src/regcompass/round1_database.py. It reads the organizer's FROZEN Round 1
# database, whose sheets are keyed by AU, MY and SG and whose answer key covers
# the nine Pillar 6 and 7 Indicators. Those literals describe a fixed historical
# artifact, not the registry the product runs on, so making them data-driven
# would misrepresent the file.
# The one-page demo and its hand-written index.html are retired;
# the interface source under ui/src is the only markup left to scan.
_SCANNED = (
    ROOT / "src" / "regcompass",
    ROOT / "ui" / "src",
)
_SKIP_DIRS = {"__pycache__", "ui_dist", "node_modules"}
_EXCLUDED_FILES = {"round1_database.py"}

_THREE_ECONOMY_TUPLE = re.compile(
    r"""[\[\(\{]\s*["'](SG|AU|MY)["']\s*,\s*["'](SG|AU|MY)["']\s*,\s*["'](SG|AU|MY)["']"""
)
_ECONOMY_LITERAL_TYPE = re.compile(r"""Literal\[\s*["'](SG|AU|MY)["']""")
_ECONOMY_UNION_TYPE = re.compile(r"""["'](SG|AU|MY)["']\s*\|\s*["'](SG|AU|MY)["']""")
# The full nine-id Round 1 set written out as a literal. A shorter run of ids
# is NOT an offender: the Pillar 6 government-data exclusion and the scoring
# rubric tables are per-indicator RULES, and the brief keeps them guarded by
# pillar rather than data-driven.
_NINE_INDICATOR_SET = re.compile(
    r"""["']6\.1["'][\s,]+["']6\.2["'][\s,]+["']6\.3["'][\s,]+["']6\.4["']"""
    r"""[\s,]+["']7\.1["'][\s,]+["']7\.2["'][\s,]+["']7\.3["']"""
    r"""[\s,]+["']7\.4["'][\s,]+["']7\.5["']"""
)


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for target in _SCANNED:
        if target.is_file():
            files.append(target)
            continue
        for path in sorted(target.rglob("*")):
            if not path.is_file() or path.suffix not in {".py", ".ts", ".tsx", ".html"}:
                continue
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            if path.name in _EXCLUDED_FILES:
                continue
            files.append(path)
    return files


class TestNoHardCodedRegistries:
    def test_the_scan_actually_reads_the_product_source(self):
        files = _scanned_files()
        assert len(files) > 20
        assert any(f.name == "cli.py" for f in files)
        assert any(f.name == "server.py" for f in files)
        assert any(f.suffix == ".tsx" for f in files)
        assert any(f.suffix == ".ts" for f in files)

    @pytest.mark.parametrize(
        "pattern,what",
        [
            (_THREE_ECONOMY_TUPLE, "a three-economy tuple"),
            (_ECONOMY_LITERAL_TYPE, "a Literal economy type"),
            (_ECONOMY_UNION_TYPE, "a union economy type"),
            (_NINE_INDICATOR_SET, "the nine-indicator literal set"),
        ],
    )
    def test_no_hard_coded_registry_survives(self, pattern, what):
        offenders = [
            str(path.relative_to(ROOT))
            for path in _scanned_files()
            if pattern.search(path.read_text(encoding="utf-8"))
        ]
        assert not offenders, f"{what} is still hard-coded in: {offenders}"

    def test_the_grep_guard_would_catch_a_regression(self, tmp_path):
        """A guard that matches nothing is worth nothing, so prove each pattern
        fires on the literal it is meant to forbid."""
        assert _THREE_ECONOMY_TUPLE.search('ECONOMIES = ("SG", "AU", "MY")')
        assert _ECONOMY_LITERAL_TYPE.search('EconomyCode = Literal["SG", "AU", "MY"]')
        assert _ECONOMY_UNION_TYPE.search("economy: 'SG' | 'AU' | 'MY'")
        assert _NINE_INDICATOR_SET.search(
            '{"6.1", "6.2", "6.3", "6.4", "7.1", "7.2", "7.3", "7.4", "7.5"}'
        )
        assert not _NINE_INDICATOR_SET.search('{"6.1", "6.2", "6.3", "6.4", "7.3"}')


# ---------------------------------------------------------------------------
# the keyword vocabularies
# ---------------------------------------------------------------------------


class TestPillarVocabularies:
    def test_every_pillar_has_exactly_its_own_indicators(self):
        pillars = load_pillars()
        indicators = load_indicators()
        for number, pillar in pillars.items():
            vocab = load_keywords(number)
            expected = {i for i in pillar.indicator_ids if indicators[i].legislation_mapped}
            assert set(vocab) == expected, number

    def test_every_vocabulary_is_thick_enough_and_carries_no_bare_stopword(self):
        from bm25s.stopwords import STOPWORDS_EN

        stopwords = frozenset(STOPWORDS_EN)
        for pillar in load_pillars():
            for key, phrases in load_keywords(pillar).items():
                assert len(phrases) >= 5, (pillar, key, len(phrases))
                for phrase in phrases:
                    assert phrase.strip()
                    assert phrase not in stopwords, (key, phrase)

    def test_every_pillar_has_a_description(self):
        for pillar in load_pillars():
            assert len(load_pillar_description(pillar)) > 50

    def test_only_the_new_pillars_are_marked_derived(self):
        for pillar in load_pillars():
            raw = json.loads(
                (CONFIG_DIR / f"pillar_{pillar}_keywords.json").read_text(encoding="utf-8")
            )
            if pillar in (6, 7):
                assert "_derived" not in raw, pillar
            else:
                assert raw["_derived"] is True, pillar
                assert raw["_reviewed"] is False, pillar

    def test_the_generator_leaves_the_curated_pillars_byte_identical(self, tmp_path):
        """Regenerating the vocabularies must never move the hand-curated
        Pillar 6 and 7 files: the Round 1 golden results depend on them."""
        import subprocess
        import sys

        work = tmp_path / "config"
        shutil.copytree(CONFIG_DIR, work)
        before = {
            p: (work / f"pillar_{p}_keywords.json").read_bytes() for p in (6, 7)
        }
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "make_pillar_keywords.py"),
             "--config-dir", str(work)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        for pillar, content in before.items():
            assert (work / f"pillar_{pillar}_keywords.json").read_bytes() == content

    def test_regenerating_the_new_pillars_is_a_no_op(self, tmp_path):
        """The committed files ARE what the generator produces, so a reviewer
        can always tell hand edits from generator output."""
        import subprocess
        import sys

        work = tmp_path / "config"
        shutil.copytree(CONFIG_DIR, work)
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "make_pillar_keywords.py"),
             "--config-dir", str(work)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        for pillar in load_pillars():
            name = f"pillar_{pillar}_keywords.json"
            assert (work / name).read_bytes() == (CONFIG_DIR / name).read_bytes(), name


# ---------------------------------------------------------------------------
# default Pillars: an unqualified Run does the mandatory pair, not all twelve
# ---------------------------------------------------------------------------


class TestDefaultPillars:
    def test_the_default_is_configuration(self):
        from regcompass.config import load_default_pillars

        assert load_default_pillars() == (6, 7)
        assert set(load_default_pillars()) < set(load_pillars())

    def test_a_default_naming_an_unconfigured_pillar_is_refused(self, config_copy):
        path = config_copy / "pillars.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["default_pillars"] = [6, 99]
        path.write_text(json.dumps(raw), encoding="utf-8")
        from regcompass.config import load_default_pillars

        with pytest.raises(ConfigInvalid, match="99"):
            load_default_pillars(config_copy)

    def test_the_command_line_default_touches_only_the_default_pillars(
        self, tmp_path, monkeypatch, no_ollama_gate
    ):
        """Omitting --pillar must not sweep 61 Indicators. The proof is the
        Indicators the Gate actually scored for the Run."""
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setattr(cli_mod, "_load_dotenv", lambda *a, **k: None)
        monkeypatch.chdir(ROOT)
        db = tmp_path / "db.sqlite"
        data = tmp_path / "data"
        _seed_sg_corpus(db, data)
        r = CliRunner().invoke(
            cli_mod.app,
            ["run", "--economy", "SG", "--engine", "fake",
             "--db", str(db), "--data-dir", str(data)],
        )
        assert r.exit_code == 0, r.output
        assert "pillars=(6, 7)" in r.output

        from regcompass.storage import Storage

        gated = Storage(db).load_gate_scores()
        assert gated, "the run must have scored some pairs"
        assert {int(i.split(".")[0]) for (_chunk, i) in gated} == {6, 7}

    def test_the_server_default_is_the_default_pillars(self, tmp_path):
        from fastapi.testclient import TestClient

        from regcompass.server import create_app

        from corpus_fixtures import seed_corpus
        from regcompass.storage import Storage

        db = tmp_path / "web.db"
        data = tmp_path / "data"
        storage = Storage(db)
        storage.apply_schema()
        seed_corpus(storage, data, "SG")  # a Run reads the Corpus; give it one
        storage.conn.close()
        client = TestClient(
            create_app(
                db_path=str(db), out_dir=tmp_path, data_dir=data,
                ui_dir=None,
            )
        )
        assert client.get("/api/status").json()["default_pillars"] == [6, 7]
        started = client.post("/api/run", json={"economy": "SG", "engine": "fake"})
        assert started.status_code in (200, 202), started.text
        assert started.json()["pillars"] == [6, 7]


# ---------------------------------------------------------------------------
# absence rows are scoped to the Pillars that actually ran
# ---------------------------------------------------------------------------


class TestAbsenceRowsFollowTheRun:
    """A "No provision found" row asserts that candidates were screened and
    found wanting. A Pillar the Gate never queried has earned no such claim."""

    def _absence(self, records, gate_lookup=None):
        from regcompass.config import load_corpus
        from regcompass.export import build_absence_rows, run_pillars_of

        return build_absence_rows(
            records,
            load_corpus(),
            load_crosswalk(),
            {"SG": {"law": "Telecommunications Act 1999", "sections": 98, "pairs_gated": 4}},
            run_pillars=run_pillars_of(records, gate_lookup),
        )

    def test_a_pillar_12_run_claims_no_pillar_6_or_7_absence(self):
        records = [_fake_record("12.4.1")]
        rows = self._absence(records, {("doc_sg_x:c0001", "12.4.1"): 0.8})
        assert rows == []

    def test_a_pillar_7_run_claims_only_pillar_7_absence(self):
        records = [_fake_record("7.4")]
        rows = self._absence(records, {("doc_sg_x:c0001", "7.4"): 0.8})
        assert {int(r["Indicator ID"].split(".")[0]) for r in rows} == {7}
        assert {r["Indicator ID"] for r in rows} == {"7.1", "7.2", "7.3", "7.5"}

    def test_a_run_with_everything_rejected_still_earns_its_zeros(self):
        """The Gate scores say what was SCREENED even when no record survived,
        so a searched Pillar still earns its absence rows."""
        rows = self._absence([], {("doc_sg_x:c0001", "7.4"): 0.8})
        assert {r["Indicator ID"] for r in rows} == {"7.1", "7.2", "7.3", "7.4", "7.5"}

    def test_with_no_records_and_no_gate_scores_the_default_pillars_stand_in(self):
        rows = self._absence([], None)
        assert {r["Indicator ID"] for r in rows} == {
            "6.1", "6.2", "6.3", "6.4", "7.1", "7.2", "7.3", "7.4", "7.5",
        }
