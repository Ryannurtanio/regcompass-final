"""The command line for the two separated lanes: `discover` fills an
Economy's Corpus, `run` reads it. Everything here is offline: Discovery is
driven over recorded Portal answers through a monkeypatched strategy fetch, and
the Run uses the fake Engine.
"""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

pytest.importorskip("httpx", reason="the discover command needs the `live` extra (httpx)")

import regcompass.cli as cli_mod  # noqa: E402
from regcompass.storage import Storage  # noqa: E402

from corpus_fixtures import seed_corpus  # noqa: E402
from forbidden_portal import (  # noqa: E402
    FORBIDDEN,
    FORBIDDEN_NAME,
    forbidden_config,
    point_loaders_at,
)
from test_discovery import (  # noqa: E402
    ACT_HTML,
    SG_SEEDS,
    NullLimiter,
    recorded_fetch,
)


@pytest.fixture()
def offline(monkeypatch):
    """No .env, no key, no live strategy: the CLI runs entirely on recorded
    answers. Returns the fetch so a test can count what was asked for."""
    import regcompass.config as config_mod
    import regcompass.discovery as discovery_mod

    monkeypatch.setattr(cli_mod, "_load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    fetch = recorded_fetch()
    real = discovery_mod.discover_economy

    def offline_discover(economy, storage, **kwargs):
        kwargs.setdefault("fetch", fetch)
        kwargs.setdefault("limiter", NullLimiter())
        kwargs.setdefault("seeds", SG_SEEDS)
        return real(economy, storage, **kwargs)

    monkeypatch.setattr(discovery_mod, "discover_economy", offline_discover)
    monkeypatch.setattr(
        config_mod, "load_crawl_seeds", lambda *a, **k: {"SG": SG_SEEDS}
    )
    return fetch


@pytest.fixture()
def forbidding(offline, tmp_path, monkeypatch):
    """The offline CLI over a config/ that carries one manual-only Portal. The
    CLI checks the Economy against the registry read at call time; Discovery
    binds its config directory as a default, so it is handed the copy."""
    import regcompass.discovery as discovery_mod

    config_dir = forbidden_config(tmp_path)
    point_loaders_at(monkeypatch, config_dir)
    offline_discover = discovery_mod.discover_economy

    def discover_under_forbidden(economy, storage, **kwargs):
        kwargs.setdefault("config_dir", config_dir)
        return offline_discover(economy, storage, **kwargs)

    monkeypatch.setattr(discovery_mod, "discover_economy", discover_under_forbidden)
    return config_dir


class TestDiscoverCommand:
    def test_discover_fills_the_corpus_and_prints_the_fetch_count(
        self, tmp_path, offline
    ):
        r = CliRunner().invoke(
            cli_mod.app,
            ["discover", "--economy", "SG", "--db", str(tmp_path / "db.sqlite"),
             "--data-dir", str(tmp_path / "data")],
        )
        assert r.exit_code == 0, r.output
        assert "fetched=2" in r.output
        assert "skipped_existing=0" in r.output
        assert "English" in r.output

        storage = Storage(tmp_path / "db.sqlite")
        rows = storage.corpus_documents("SG")
        assert len(rows) == 2
        assert {row["source_url"] for row in rows} == set(ACT_HTML)
        assert all(row["language"] == "English" for row in rows)

    def test_a_second_discover_asks_for_nothing_and_refresh_re_fetches(
        self, tmp_path, offline
    ):
        args = [
            "discover", "--economy", "SG", "--db", str(tmp_path / "db.sqlite"),
            "--data-dir", str(tmp_path / "data"),
        ]
        runner = CliRunner()
        assert runner.invoke(cli_mod.app, args).exit_code == 0
        asked = len(offline.calls)

        r = runner.invoke(cli_mod.app, args)
        assert r.exit_code == 0, r.output
        assert "fetched=0" in r.output and "skipped_existing=2" in r.output
        assert len(offline.calls) == asked, "nothing already in the Corpus is re-asked"

        r = runner.invoke(cli_mod.app, [*args, "--refresh"])
        assert r.exit_code == 0, r.output
        assert "fetched=2" in r.output
        assert len(offline.calls) == asked * 2

    def test_a_fetched_file_with_no_corpus_row_is_said_out_loud(
        self, tmp_path, offline
    ):
        """The summary counts Corpus rows, and names the gap when the fetch
        read more files than the Corpus ended up holding."""
        db = tmp_path / "db.sqlite"
        storage = Storage(db)
        storage.apply_schema()
        url = "https://sso.agc.gov.sg/Act/LOST2020?ViewType=Pdf"
        storage.manifest_add_pending(url, "SG", filename_hint="lost.pdf")
        storage.manifest_mark_fetched(
            url, http_status=200, method="httpx", sha256="0" * 64,
            content_type="application/pdf", size_bytes=3,
            local_path="SG/raw/lost.pdf",
        )
        storage.close()

        r = CliRunner().invoke(
            cli_mod.app,
            ["discover", "--economy", "SG", "--db", str(db),
             "--data-dir", str(tmp_path / "data")],
        )
        assert r.exit_code == 0, r.output
        assert "stored=2" in r.output, "stored counts rows, not files read"
        assert "1 fetched file(s) have no Corpus row" in r.output

    def test_a_manual_only_economy_is_refused_with_exit_2(self, tmp_path, forbidding):
        r = CliRunner().invoke(
            cli_mod.app,
            ["discover", "--economy", FORBIDDEN, "--db", str(tmp_path / "db.sqlite"),
             "--data-dir", str(tmp_path / "data")],
        )
        assert r.exit_code == 2
        assert FORBIDDEN_NAME in r.output and "Add document" in r.output
        assert "manual-only" in r.output


class TestRunReadsTheCorpus:
    def test_run_on_an_empty_corpus_exits_2_and_names_the_next_command(self, tmp_path, offline):
        r = CliRunner().invoke(
            cli_mod.app,
            ["run", "--economy", "SG", "--pillar", "7", "--engine", "fake",
             "--db", str(tmp_path / "db.sqlite"), "--data-dir", str(tmp_path / "data")],
        )
        assert r.exit_code == 2, r.output
        assert "no Documents in the Corpus for Singapore" in r.output
        assert "regcompass discover --economy SG" in r.output

    def test_discover_then_run_maps_the_corpus(self, tmp_path, monkeypatch, offline):
        db = tmp_path / "db.sqlite"
        data = tmp_path / "data"
        runner = CliRunner()
        assert runner.invoke(
            cli_mod.app,
            ["discover", "--economy", "SG", "--db", str(db), "--data-dir", str(data)],
        ).exit_code == 0

        monkeypatch.chdir(tmp_path)  # config resolution must not need the repo cwd
        import regcompass.gate as gate_mod

        def boom(*a, **k):
            raise AssertionError("the fake Engine must never reach Ollama")

        monkeypatch.setattr(gate_mod, "embed_ollama", boom)
        r = runner.invoke(
            cli_mod.app,
            ["run", "--economy", "SG", "--pillar", "7", "--engine", "fake",
             "--db", str(db), "--data-dir", str(data)],
        )
        assert r.exit_code == 0, r.output
        assert "2 documents" in r.output
        # the Run read exactly the Documents Discovery stored, and nothing else
        storage = Storage(db)
        extracted = {
            row["decision"]
            for row in storage.conn.execute(
                "SELECT decision FROM audit_log WHERE stage = 'm1_extract'"
            )
        }
        assert extracted, "the Run must have extracted the Corpus Documents"
        assert len(storage.corpus_documents("SG")) == 2


def plain(text: str) -> str:
    """The help text without terminal styling. Under a CI runner the help
    renderer colours every flag, and the codes fall between the dashes and the
    name, so `--refresh` is never one contiguous string until they are gone."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


class TestE2EHelpNamesTheTwoLanes:
    def test_the_help_says_discovery_then_run(self):
        r = CliRunner().invoke(cli_mod.app, ["e2e", "--help"])
        assert r.exit_code == 0
        out = plain(r.output)
        assert "Discovery" in out and "Run" in out

    def test_discover_help_names_the_refresh_flag(self):
        r = CliRunner().invoke(cli_mod.app, ["discover", "--help"])
        assert r.exit_code == 0
        assert "--refresh" in plain(r.output)


class TestSeededCorpusIsRunnable:
    def test_a_corpus_seeded_outside_the_cli_runs(self, tmp_path, monkeypatch):
        """The Run lane cares about the Corpus, not about who filled it: a
        Corpus put there by "Add document" or by a shipped database runs the
        same way a discovered one does."""
        monkeypatch.setattr(cli_mod, "_load_dotenv", lambda *a, **k: None)
        db = tmp_path / "seeded.db"
        storage = Storage(db)
        storage.apply_schema()
        seed_corpus(storage, tmp_path / "data", "MY")
        storage.close()
        r = CliRunner().invoke(
            cli_mod.app,
            ["run", "--economy", "MY", "--pillar", "7", "--engine", "fake",
             "--db", str(db), "--data-dir", str(tmp_path / "data")],
        )
        assert r.exit_code == 0, r.output
        assert "1 documents" in r.output


class TestCrawlIsADeprecatedAliasOfDiscover:
    """Discovery is the ONLY step that touches the internet. The old
    `crawl` command reached the fetch machinery directly, which meant no single
    connection, no robots Disallow check and no Discovery record. It is now the
    same lane under an old name."""

    def test_it_says_it_is_deprecated_and_names_discover(self, tmp_path, offline):
        r = CliRunner().invoke(
            cli_mod.app,
            ["crawl", "--economy", "SG", "--db", str(tmp_path / "db.sqlite"),
             "--data-dir", str(tmp_path / "data")],
        )
        assert r.exit_code == 0, r.output
        assert "deprecated" in r.output.lower()
        assert "regcompass discover" in r.output

    def test_it_fills_the_corpus_and_files_a_discovery_record(self, tmp_path, offline):
        db = tmp_path / "db.sqlite"
        r = CliRunner().invoke(
            cli_mod.app,
            ["crawl", "--economy", "SG", "--db", str(db),
             "--data-dir", str(tmp_path / "data")],
        )
        assert r.exit_code == 0, r.output
        storage = Storage(db)
        assert len(storage.corpus_documents("SG")) == 2
        records = storage.runs_list(kind="discovery")
        assert len(records) == 1 and records[0]["documents_fetched"] == 2

    def test_a_manual_only_economy_is_refused_here_too(self, tmp_path, forbidding):
        r = CliRunner().invoke(
            cli_mod.app,
            ["crawl", "--economy", FORBIDDEN, "--db", str(tmp_path / "db.sqlite"),
             "--data-dir", str(tmp_path / "data")],
        )
        assert r.exit_code == 2
        assert FORBIDDEN_NAME in r.output and "manual-only" in r.output
