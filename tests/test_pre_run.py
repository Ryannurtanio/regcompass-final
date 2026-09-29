"""The pre-run job: resumable, ledgered, and stopped by its budget.

Nothing here spends a cent or opens a socket. Every test injects its own
executor in place of `regcompass run`, so what is under test is the job's
bookkeeping: which combinations it skips, when it refuses to start, when it
stops, and what it writes down. The injected executor writes the same Run
Record a real Run leaves, because the ledger and the report read nothing else.

The one test that names a hosted Engine (engine-b) names it for its PRICE, not
its key: the estimate is what the ceiling guard compares against, and no call
is ever made. That is why these tests carry no paid marker.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from regcompass.storage import Storage

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pre_run  # noqa: E402

START = "2026-09-16T00:00:00Z"
END = "2026-09-16T00:02:00Z"  # 120.0 s, so the ledger's wall time is checkable


@pytest.fixture()
def db(tmp_path) -> Path:
    """An empty working database with the runs table, as the CLI would leave
    it after its first `storage.apply_schema()`."""
    path = tmp_path / "work.db"
    storage = Storage(path)
    storage.apply_schema()
    storage.conn.close()
    return path


def write_run(
    db: Path,
    economy: str,
    pillar: int,
    engine: str,
    *,
    run_id: str,
    cost_usd: float = 0.0,
    documents: int = 3,
    mappings: int = 7,
    calls: int = 11,
    status: str = "completed",
    indicators: list[str] | None = None,
) -> None:
    """The Run Record a real Run of this combination would have left."""
    storage = Storage(db)
    storage.apply_schema()
    storage.run_start(
        run_id=run_id, kind="run", economy=economy, pillars=[pillar],
        indicators=indicators, engine=engine, started_at=START,
    )
    storage.run_finish(
        run_id, status=status, ended_at=END, cost_usd=cost_usd,
        details={
            "documents": [f"doc_{i}" for i in range(documents)],
            "n_passed": mappings,
            "model_calls": calls,
        },
    )
    storage.conn.close()


class FakeRuns:
    """Stands in for `regcompass run`: records the combination it was asked
    for and leaves the Run Record that Run would have left, at a cost the test
    chooses. Never starts a process and never touches a network."""

    def __init__(self, costs: dict | float = 0.0, *, exit_code: int = 0, record=True):
        self.costs = costs
        self.exit_code = exit_code
        self.record = record
        self.calls: list = []
        self.run_ids: list[str] = []

    def __call__(self, combo, *, db, data_dir, concurrency):
        self.calls.append(combo)
        cost = (
            self.costs
            if isinstance(self.costs, (int, float))
            else self.costs.get(combo.key, 0.0)
        )
        # The id carries the combination, so two jobs against the same database
        # cannot collide on the runs table's primary key.
        run_id = f"run_fake_{combo.economy}_{combo.pillar}_{combo.engine}"
        self.run_ids.append(run_id)
        if self.record:
            write_run(
                db, combo.economy, combo.pillar, combo.engine,
                run_id=run_id, cost_usd=float(cost),
            )
        return self.exit_code


def run_main(args: list[str], executor) -> int:
    return pre_run.main(args, execute=executor)


class TestThePlan:
    def test_the_order_is_economy_then_pillar_then_engine(self):
        combos = pre_run.plan_combos(("SG", "AU"), (7, 6), ("engine-a", "engine-b"))
        assert [str(c) for c in combos] == [
            "SG P6 engine-a", "SG P6 engine-b", "SG P7 engine-a", "SG P7 engine-b",
            "AU P6 engine-a", "AU P6 engine-b", "AU P7 engine-a", "AU P7 engine-b",
        ]

    def test_the_default_economies_are_the_prepared_ones(self):
        """The default is data, not a literal: portals.yaml decides which
        Economies have a Corpus worth running."""
        from regcompass.config import load_portals

        prepared = pre_run.prepared_economies()
        assert prepared == tuple(
            code for code, p in load_portals().items() if p.prepared
        )
        assert set(prepared) >= {"AU", "MY", "SG"}


class TestSkippingWhatIsDone:
    def test_a_completed_run_record_is_skipped(self, db, tmp_path, capsys):
        write_run(db, "SG", 7, "fake", run_id="run_earlier")
        fake = FakeRuns()
        code = run_main(
            [
                "--db", str(db), "--data-dir", str(tmp_path),
                "--ledger", str(tmp_path / "ledger.tsv"),
                "--economies", "SG", "--pillars", "7", "--engines", "fake",
                "--ceiling-usd", "1", "--yes",
            ],
            fake,
        )
        assert code == 0
        assert fake.calls == []
        out = capsys.readouterr().out
        assert "skip SG P7 fake: completed Run run_earlier" in out
        assert "nothing to run" in out

    def test_a_run_narrowed_to_indicators_does_not_count_as_coverage(self, db, tmp_path):
        """The sealed live test runs two Indicators of one Pillar. That Run
        answers a different question, so the combination is still to do."""
        write_run(
            db, "SG", 7, "fake", run_id="run_narrow", indicators=["7.1", "7.2"]
        )
        fake = FakeRuns()
        code = run_main(
            [
                "--db", str(db), "--data-dir", str(tmp_path),
                "--ledger", str(tmp_path / "ledger.tsv"),
                "--economies", "SG", "--pillars", "7", "--engines", "fake",
                "--ceiling-usd", "1", "--yes",
            ],
            fake,
        )
        assert code == 0
        assert [str(c) for c in fake.calls] == ["SG P7 fake"]

    def test_a_failed_run_record_does_not_count_as_coverage(self, db, tmp_path):
        write_run(db, "SG", 7, "fake", run_id="run_dead", status="failed")
        fake = FakeRuns()
        run_main(
            [
                "--db", str(db), "--data-dir", str(tmp_path),
                "--ledger", str(tmp_path / "ledger.tsv"),
                "--economies", "SG", "--pillars", "7", "--engines", "fake",
                "--ceiling-usd", "1", "--yes",
            ],
            fake,
        )
        assert [str(c) for c in fake.calls] == ["SG P7 fake"]


class TestTheLedger:
    def test_one_run_appends_one_line_of_its_own_figures(self, db, tmp_path):
        ledger = tmp_path / "ledger.tsv"
        fake = FakeRuns(0.25)
        code = run_main(
            [
                "--db", str(db), "--data-dir", str(tmp_path), "--ledger", str(ledger),
                "--economies", "SG", "--pillars", "7", "--engines", "fake",
                "--ceiling-usd", "1", "--yes",
            ],
            fake,
        )
        assert code == 0
        lines = ledger.read_text(encoding="utf-8").splitlines()
        assert lines[0].split("\t") == list(pre_run.LEDGER_COLUMNS)
        assert len(lines) == 2
        fields = lines[1].split("\t")
        assert fields[1:4] == ["SG", "7", "fake"]
        assert fields[4] == fake.run_ids[0]
        assert fields[5:] == ["3", "7", "120.0", "11", "0.25"]

        rows = pre_run.read_ledger(ledger)
        assert rows[0]["documents"] == 3
        assert rows[0]["mappings"] == 7
        assert rows[0]["wall_s"] == 120.0
        assert rows[0]["engine_calls"] == 11
        assert rows[0]["usd"] == 0.25

    def test_a_second_job_appends_rather_than_rewrites(self, db, tmp_path):
        ledger = tmp_path / "ledger.tsv"
        for pillar in (6, 7):
            run_main(
                [
                    "--db", str(db), "--data-dir", str(tmp_path),
                    "--ledger", str(ledger), "--economies", "SG",
                    "--pillars", str(pillar), "--engines", "fake",
                    "--ceiling-usd", "1", "--yes",
                ],
                FakeRuns(),
            )
        rows = pre_run.read_ledger(ledger)
        assert [(r["economy"], r["pillar"]) for r in rows] == [("SG", 6), ("SG", 7)]


class TestTheMoneyGuards:
    def test_a_hosted_engine_is_refused_without_paid(self, db, tmp_path, capsys):
        fake = FakeRuns()
        code = run_main(
            [
                "--db", str(db), "--data-dir", str(tmp_path),
                "--ledger", str(tmp_path / "ledger.tsv"),
                "--economies", "SG", "--pillars", "7", "--engines", "engine-b",
                "--ceiling-usd", "1", "--yes",
            ],
            fake,
        )
        assert code == 2
        assert fake.calls == []
        assert "--paid" in capsys.readouterr().out

    def test_the_ceiling_is_required(self, db, tmp_path, capsys):
        code = run_main(
            ["--db", str(db), "--engines", "fake", "--yes"], FakeRuns()
        )
        assert code == 2
        assert "--ceiling-usd is required" in capsys.readouterr().out

    def test_the_ceiling_stops_before_the_run_that_would_cross_it(
        self, db, tmp_path, capsys
    ):
        """SG Pillar 6 on engine-b is estimated at USD 0.075 and Pillar 7 at
        0.087 (the measured figures). With USD 0.05 recorded by the first Run, a
        ceiling of 0.10 cannot fit the second, so it must not start."""
        ledger = tmp_path / "ledger.tsv"
        fake = FakeRuns(0.05)
        code = run_main(
            [
                "--db", str(db), "--data-dir", str(tmp_path), "--ledger", str(ledger),
                "--economies", "SG", "--pillars", "6,7", "--engines", "engine-b",
                "--ceiling-usd", "0.10", "--paid", "--yes",
            ],
            fake,
        )
        assert code == 3
        assert [str(c) for c in fake.calls] == ["SG P6 engine-b"]
        out = capsys.readouterr().out
        assert "STOP before SG P7 engine-b" in out
        assert "ceiling of USD 0.10" in out
        assert len(pre_run.read_ledger(ledger)) == 1

    def test_the_ceiling_counts_what_earlier_jobs_already_spent(
        self, db, tmp_path, capsys
    ):
        """Resumability must not reset the budget: the ledger is the authority
        on what has been paid, not this process."""
        ledger = tmp_path / "ledger.tsv"
        pre_run.append_ledger(
            ledger,
            {
                "timestamp": START, "economy": "AU", "pillar": 6,
                "engine": "engine-b", "run_id": "run_yesterday", "documents": 18,
                "mappings": 40, "wall_s": 60.0, "engine_calls": 100, "usd": 0.09,
            },
        )
        fake = FakeRuns(0.01)
        code = run_main(
            [
                "--db", str(db), "--data-dir", str(tmp_path), "--ledger", str(ledger),
                "--economies", "SG", "--pillars", "6", "--engines", "engine-b",
                "--ceiling-usd", "0.10", "--paid", "--yes",
            ],
            fake,
        )
        assert code == 3
        assert fake.calls == []
        assert "STOP before SG P6 engine-b" in capsys.readouterr().out

    def test_a_single_expensive_run_stops_the_job_after_recording_it(
        self, db, tmp_path, capsys
    ):
        ledger = tmp_path / "ledger.tsv"
        fake = FakeRuns({("SG", 6, "engine-b"): 4.0})
        code = run_main(
            [
                "--db", str(db), "--data-dir", str(tmp_path), "--ledger", str(ledger),
                "--economies", "SG", "--pillars", "6,7", "--engines", "engine-b",
                "--ceiling-usd", "35", "--max-run-usd", "3", "--paid", "--yes",
            ],
            fake,
        )
        assert code == 3
        assert [str(c) for c in fake.calls] == ["SG P6 engine-b"]
        out = capsys.readouterr().out
        assert "STOP after SG P6 engine-b" in out
        assert "over --max-run-usd" in out
        rows = pre_run.read_ledger(ledger)
        assert [r["usd"] for r in rows] == [4.0]

    def test_a_failing_run_stops_the_job(self, db, tmp_path, capsys):
        fake = FakeRuns(exit_code=1, record=False)
        code = run_main(
            [
                "--db", str(db), "--data-dir", str(tmp_path),
                "--ledger", str(tmp_path / "ledger.tsv"),
                "--economies", "SG", "--pillars", "6,7", "--engines", "fake",
                "--ceiling-usd", "1", "--yes",
            ],
            fake,
        )
        assert code == 1
        assert len(fake.calls) == 1
        assert "exited 1" in capsys.readouterr().out

    def test_a_run_that_leaves_no_record_stops_the_job(self, db, tmp_path, capsys):
        fake = FakeRuns(record=False)
        code = run_main(
            [
                "--db", str(db), "--data-dir", str(tmp_path),
                "--ledger", str(tmp_path / "ledger.tsv"),
                "--economies", "SG", "--pillars", "6,7", "--engines", "fake",
                "--ceiling-usd", "1", "--yes",
            ],
            fake,
        )
        assert code == 1
        assert "left no completed Run Record" in capsys.readouterr().out


class TestTheEstimate:
    def test_the_measured_combinations_use_the_measured_figures(self):
        combo = pre_run.Combo("AU", 6, "engine-a")
        assert pre_run.estimate_usd(
            combo, api_key_env="OPENROUTER_API_KEY", open_weights=False
        ) == pytest.approx(0.962)

    def test_a_keyless_engine_is_free_by_construction(self):
        combo = pre_run.Combo("AU", 6, "fake")
        assert pre_run.estimate_usd(combo, api_key_env=None, open_weights=False) == 0.0

    def test_an_unmeasured_economy_is_priced_per_document(self):
        """Lao PDR has no measurement, so the estimate is the Gate's cap: the
        per-Document rate times the Pillar's Indicator count."""
        combo = pre_run.Combo("LA", 6, "engine-a")
        got = pre_run.estimate_usd(
            combo, api_key_env="OPENROUTER_API_KEY", open_weights=False, documents=12
        )
        assert got == pytest.approx((0.052 / 4) * 4 * 12)


class TestTheReport:
    def test_the_table_is_markdown_with_a_row_per_combination(self, tmp_path):
        ledger = tmp_path / "ledger.tsv"
        for economy, pillar, engine, usd, mappings in (
            ("SG", 6, "engine-a", 0.5, 20),
            ("SG", 7, "engine-b", 0.25, 10),
        ):
            pre_run.append_ledger(
                ledger,
                {
                    "timestamp": START, "economy": economy, "pillar": pillar,
                    "engine": engine, "run_id": f"run_{economy}{pillar}",
                    "documents": 10, "mappings": mappings, "wall_s": 120.0,
                    "engine_calls": 99, "usd": usd,
                },
            )
        report = pre_run.render_report(pre_run.read_ledger(ledger))
        lines = report.splitlines()
        assert lines[0] == (
            "| Economy | Pillar | Engine | Documents | Mappings | USD | Minutes |"
        )
        assert lines[1] == "|---|---|---|---|---|---|---|"
        assert lines[2] == "| SG | 6 | engine-a | 10 | 20 | 0.5000 | 2.0 |"
        assert lines[3] == "| SG | 7 | engine-b | 10 | 10 | 0.2500 | 2.0 |"
        assert lines[4] == "| **Total** | | | | 30 | 0.7500 | 4.0 |"
        assert "USD 0.7500 spent in total" in report

    def test_a_rerun_combination_shows_once_but_counts_twice(self, tmp_path):
        """The database holds the latest Run's records, so the table shows the
        latest line. The money is still the sum: both Runs were paid for."""
        ledger = tmp_path / "ledger.tsv"
        for run_id, mappings, usd in (("run_1", 20, 0.5), ("run_2", 22, 0.6)):
            pre_run.append_ledger(
                ledger,
                {
                    "timestamp": START, "economy": "SG", "pillar": 6,
                    "engine": "engine-a", "run_id": run_id, "documents": 10,
                    "mappings": mappings, "wall_s": 60.0, "engine_calls": 99,
                    "usd": usd,
                },
            )
        report = pre_run.render_report(pre_run.read_ledger(ledger))
        assert "| SG | 6 | engine-a | 10 | 22 | 0.6000 | 1.0 |" in report
        assert "2 Run(s) in the ledger, USD 1.1000 spent in total" in report

    def test_report_prints_the_table_and_runs_nothing(self, tmp_path, capsys):
        ledger = tmp_path / "ledger.tsv"
        pre_run.append_ledger(
            ledger,
            {
                "timestamp": START, "economy": "SG", "pillar": 6, "engine": "fake",
                "run_id": "run_1", "documents": 10, "mappings": 20, "wall_s": 60.0,
                "engine_calls": 99, "usd": 0.0,
            },
        )
        fake = FakeRuns()
        code = run_main(["--ledger", str(ledger), "--report"], fake)
        assert code == 0
        assert fake.calls == []
        assert "| SG | 6 | fake | 10 | 20 | 0.0000 | 1.0 |" in capsys.readouterr().out

    def test_an_empty_ledger_says_so_instead_of_printing_an_empty_table(self):
        assert pre_run.render_report([]) == "No Runs in the ledger yet."


class TestTheCommandItRuns:
    def test_it_shells_out_to_regcompass_run_with_one_pillar_and_one_engine(
        self, tmp_path
    ):
        """The job must not grow its own pipeline: what it runs is the CLI a
        reviewer runs, one Pillar and one Engine at a time."""
        cmd = pre_run.cli_command(
            pre_run.Combo("SG", 7, "engine-a"),
            db=tmp_path / "work.db", data_dir=tmp_path / "data", concurrency=4,
        )
        assert cmd[-13:] == [
            "run",
            "--economy", "SG",
            "--pillar", "7",
            "--engine", "engine-a",
            "--db", str(tmp_path / "work.db"),
            "--data-dir", str(tmp_path / "data"),
            "--concurrency", "4",
        ]
        assert cmd[cmd.index("--economy") + 1] == "SG"
        assert cmd[cmd.index("--pillar") + 1] == "7"
        assert cmd[cmd.index("--engine") + 1] == "engine-a"
        assert cmd[cmd.index("--concurrency") + 1] == "4"

    def test_concurrency_is_left_to_the_engine_when_not_asked_for(self, tmp_path):
        cmd = pre_run.cli_command(
            pre_run.Combo("SG", 7, "fake"),
            db=tmp_path / "work.db", data_dir=tmp_path / "data", concurrency=None,
        )
        assert "--concurrency" not in cmd

    def test_the_job_sets_no_environment_variable_of_its_own(self):
        """REGCOMPASS_PAID is the pytest opt-in switch, not a runtime flag, and
        --paid must not quietly set it (or anything else) for a child process."""
        source = (ROOT / "scripts" / "pre_run.py").read_text(encoding="utf-8")
        assert "os.environ" not in source
        assert "REGCOMPASS_PAID" in source  # named in the docstring, never set
        assert "putenv" not in source
