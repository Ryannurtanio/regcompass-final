"""`regcompass seed` on the command line: the offline step in front of the
keyless demo.

The command is what the compose demo service and the README both call, so its
contract is checked here rather than only through the container: it fills one
Economy's Corpus (or every bundled one), it is safe to run twice, it refuses an
Economy with no bundled legislation instead of guessing, and an empty Corpus
now points a judge at it rather than only at a network Discovery.
"""

from __future__ import annotations

from typer.testing import CliRunner

import regcompass.cli as cli_mod
from regcompass.corpus import FIXTURE_SOURCE_KIND
from regcompass.storage import Storage


def _invoke(*args):
    return CliRunner().invoke(cli_mod.app, list(args))


class TestSeedingOneEconomy:
    def test_it_fills_the_corpus_and_names_what_it_added(self, tmp_path):
        db = tmp_path / "db.sqlite"
        r = _invoke(
            "seed", "--economy", "SG", "--db", str(db),
            "--data-dir", str(tmp_path / "data"),
        )
        assert r.exit_code == 0, r.output
        assert "doc_sg_telecommunications_act_1999" in r.output
        rows = Storage(db).corpus_documents("SG")
        assert [row["source_kind"] for row in rows] == [FIXTURE_SOURCE_KIND]

    def test_it_says_the_corpus_is_a_demo_corpus(self, tmp_path):
        """A judge has to be able to tell this Corpus from a collected one
        without reading the database."""
        r = _invoke(
            "seed", "--economy", "SG", "--db", str(tmp_path / "db.sqlite"),
            "--data-dir", str(tmp_path / "data"),
        )
        assert "fixture legislation, demo only" in r.output

    def test_seeding_twice_adds_nothing_and_still_exits_zero(self, tmp_path):
        db = tmp_path / "db.sqlite"
        data = tmp_path / "data"
        first = _invoke("seed", "--economy", "SG", "--db", str(db), "--data-dir", str(data))
        assert first.exit_code == 0, first.output
        second = _invoke("seed", "--economy", "SG", "--db", str(db), "--data-dir", str(data))
        assert second.exit_code == 0, second.output
        assert "already seeded" in second.output
        assert len(Storage(db).corpus_documents("SG")) == 1

    def test_an_unknown_economy_code_is_refused(self, tmp_path):
        r = _invoke(
            "seed", "--economy", "ZZ", "--db", str(tmp_path / "db.sqlite"),
            "--data-dir", str(tmp_path / "data"),
        )
        assert r.exit_code == 2, r.output

    def test_an_economy_with_no_bundled_legislation_names_the_ones_that_have_some(
        self, tmp_path
    ):
        r = _invoke(
            "seed", "--economy", "LA", "--db", str(tmp_path / "db.sqlite"),
            "--data-dir", str(tmp_path / "data"),
        )
        assert r.exit_code == 2, r.output
        assert "AU" in r.output and "SG" in r.output


class TestSeedingEveryBundledEconomy:
    def test_all_fixtures_seeds_each_one(self, tmp_path):
        db = tmp_path / "db.sqlite"
        r = _invoke(
            "seed", "--all-fixtures", "--db", str(db),
            "--data-dir", str(tmp_path / "data"),
        )
        assert r.exit_code == 0, r.output
        storage = Storage(db)
        for economy in ("AU", "MY", "SG"):
            assert storage.corpus_documents(economy), f"{economy} was not seeded"

    def test_naming_neither_economy_nor_all_fixtures_is_refused(self, tmp_path):
        r = _invoke("seed", "--db", str(tmp_path / "db.sqlite"))
        assert r.exit_code == 2, r.output
        assert "--economy" in r.output and "--all-fixtures" in r.output


class TestAnEmptyCorpusPointsAtTheOfflineStep:
    def test_a_run_with_no_corpus_names_seed_as_well_as_discover(self, tmp_path, monkeypatch):
        """Defect 2 of the Docker work: the keyless demo sent a judge with no network
        to `regcompass discover`. The offline step has to be on that line."""
        monkeypatch.setattr(cli_mod, "_load_dotenv", lambda *a, **k: None)
        r = _invoke(
            "run", "--economy", "SG", "--pillar", "7", "--engine", "fake",
            "--db", str(tmp_path / "db.sqlite"), "--data-dir", str(tmp_path / "data"),
        )
        assert r.exit_code == 2, r.output
        assert "regcompass discover --economy SG" in r.output
        assert "regcompass seed --economy SG" in r.output

    def test_the_seed_hint_is_left_off_an_economy_with_no_bundled_legislation(
        self, tmp_path, monkeypatch
    ):
        """Pointing a judge at a command that would refuse them is worse than
        pointing at nothing."""
        monkeypatch.setattr(cli_mod, "_load_dotenv", lambda *a, **k: None)
        r = _invoke(
            "run", "--economy", "LA", "--pillar", "7", "--engine", "fake",
            "--db", str(tmp_path / "db.sqlite"), "--data-dir", str(tmp_path / "data"),
        )
        assert r.exit_code == 2, r.output
        assert "regcompass seed" not in r.output
