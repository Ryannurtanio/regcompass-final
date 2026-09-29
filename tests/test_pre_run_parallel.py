"""The pre-run job with several Runs going at once (`--parallel N`).

Most of these tests inject a launcher in place of `regcompass run`: it hands
back a stand-in process that finishes after a number of polls the test
chooses, and when it finishes it leaves the Run Record a real Run would have
left. What is under test is the parent's bookkeeping: what starts when, what
the money guard counts, what the ledger gets and what the job exits with.
Nothing here opens a socket or spends a cent.

One test is different on purpose. It starts real `regcompass run` processes
on the fake Engine against one scratch database at the same time, because the
claim that matters most (two Runs writing one database at once both finish,
and each exports exactly what it would have exported alone) is a claim about
SQLite and the Run path, not about this script.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import pytest

from regcompass.pipeline import export_from_db
from regcompass.storage import Storage

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pre_run  # noqa: E402
from test_pre_run import FakeRuns, write_run  # noqa: E402


@pytest.fixture()
def db(tmp_path) -> Path:
    path = tmp_path / "work.db"
    storage = Storage(path)
    storage.apply_schema()
    storage.close()
    return path


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """The parent sleeps between polls; a test has no reason to."""
    monkeypatch.setattr(pre_run, "_sleep", lambda seconds: None)


class FakeProcess:
    """What the launcher hands back: finishes on its `polls`-th poll."""

    def __init__(self, launcher, combo, db, polls, exit_code, cost, record, details=None):
        self.launcher = launcher
        self.details = details
        self.combo = combo
        self.db = db
        self.polls_left = polls
        self.exit_code = exit_code
        self.cost = cost
        self.record = record
        self.returncode = None
        self.terminated = False

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        self.polls_left -= 1
        if self.polls_left > 0:
            return None
        self._finish(self.exit_code)
        return self.returncode

    def _finish(self, code):
        self.returncode = code
        self.launcher.running.discard(self.combo)
        self.launcher.finished.append(str(self.combo))
        if code == 0 and self.record and self.details is not None:
            write_run_with_details(self.db, self.combo, self.details, cost=self.cost)
        elif code == 0 and self.record:
            write_run(
                self.db, self.combo.economy, self.combo.pillar, self.combo.engine,
                run_id=f"run_fake_{self.combo.economy}_{self.combo.pillar}_{self.combo.engine}",
                cost_usd=float(self.cost),
            )

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15
            self.launcher.running.discard(self.combo)

    def kill(self):  # pragma: no cover - terminate always suffices here
        self.terminate()

    def wait(self, timeout=None):
        return self.returncode


class FakeLauncher:
    """Stands in for starting `regcompass run` as a child process."""

    def __init__(
        self, *, costs=None, polls=None, fail=None, no_record=(), default_polls=2,
        details=None, log_text=None, raise_on_call=None,
    ):
        self.details = details or {}
        self.log_text = log_text or {}
        self.raise_on_call = raise_on_call
        self.costs = costs or {}
        self.polls = polls or {}
        self.fail = fail or {}
        self.no_record = set(no_record)
        self.default_polls = default_polls
        self.started: list[str] = []
        self.finished: list[str] = []
        self.running: set = set()
        self.max_running = 0
        self.running_at_start: dict[str, set[str]] = {}
        self.log_paths: dict[str, Path] = {}
        self.processes: list[FakeProcess] = []

    def __call__(self, combo, *, db, data_dir, concurrency, log_path):
        if self.raise_on_call is not None and len(self.started) + 1 == self.raise_on_call:
            raise OSError("could not start regcompass run")
        name = str(combo)
        if combo.key in self.log_text:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            Path(log_path).write_text(self.log_text[combo.key], encoding="utf-8")
        self.running_at_start[name] = {str(c) for c in self.running}
        self.started.append(name)
        self.log_paths[name] = Path(log_path)
        self.running.add(combo)
        self.max_running = max(self.max_running, len(self.running))
        process = FakeProcess(
            self, combo, db,
            polls=self.polls.get(combo.key, self.default_polls),
            exit_code=self.fail.get(combo.key, 0),
            cost=self.costs.get(combo.key, 0.0),
            record=combo.key not in self.no_record,
            details=self.details.get(combo.key),
        )
        self.processes.append(process)
        return process


def write_run_with_details(db: Path, combo, details: dict, *, cost: float = 0.0) -> None:
    """A completed Run Record carrying these details, as a Run leaves it."""
    storage = Storage(db)
    run_id = f"run_fake_{combo.economy}_{combo.pillar}_{combo.engine}"
    storage.run_start(
        run_id=run_id, kind="run", economy=combo.economy, pillars=[combo.pillar],
        indicators=None, engine=combo.engine, started_at="2026-09-16T00:00:00Z",
    )
    storage.run_finish(
        run_id, status="completed", ended_at="2026-09-16T00:02:00Z",
        cost_usd=cost, details=details,
    )
    storage.close()


def no_launch(*args, **kwargs):  # pragma: no cover - failing is the assertion
    raise AssertionError("--parallel 1 must not use the parallel launcher")


def args_for(db: Path, tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--db", str(db), "--data-dir", str(tmp_path),
        "--ledger", str(tmp_path / "ledger.tsv"), "--yes", *extra,
    ]


# ---------------------------------------------------------------------------
# --parallel 1 is today's job, unchanged
# ---------------------------------------------------------------------------


def _normalised(text: str, tmp_path: Path) -> str:
    text = text.replace(str(tmp_path), "<tmp>")
    return re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", "<ts>", text)


class TestParallelOneIsTheSequentialJob:
    def test_the_output_is_the_same_as_without_the_flag(self, tmp_path, capsys):
        outputs = []
        for flag in ([], ["--parallel", "1"]):
            root = tmp_path / ("flag" if flag else "plain")
            root.mkdir()
            db = root / "work.db"
            s = Storage(db)
            s.apply_schema()
            s.close()
            code = pre_run.main(
                args_for(
                    db, root, "--economies", "SG,MY", "--pillars", "6,7",
                    "--engines", "fake", "--ceiling-usd", "1", *flag,
                ),
                execute=FakeRuns(),
                launch=no_launch,
            )
            assert code == 0
            outputs.append(_normalised(capsys.readouterr().out, root))
        assert outputs[0] == outputs[1]

    def test_the_default_is_one(self):
        assert pre_run.build_parser().parse_args([]).parallel == 1

    def test_zero_is_refused(self, db, tmp_path, capsys):
        code = pre_run.main(
            args_for(db, tmp_path, "--engines", "fake", "--ceiling-usd", "1",
                     "--parallel", "0"),
            execute=FakeRuns(), launch=no_launch,
        )
        assert code == 2
        assert "--parallel" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# what runs when
# ---------------------------------------------------------------------------


class TestSeveralRunsAtOnce:
    def test_it_keeps_n_running_and_runs_everything(self, db, tmp_path, capsys):
        launcher = FakeLauncher()
        code = pre_run.main(
            args_for(db, tmp_path, "--economies", "SG,MY", "--pillars", "6,7",
                     "--engines", "fake", "--ceiling-usd", "1", "--parallel", "3"),
            launch=launcher,
        )
        assert code == 0
        assert launcher.max_running == 3
        assert sorted(launcher.started) == sorted(launcher.finished)
        assert len(launcher.started) == 4
        rows = pre_run.read_ledger(tmp_path / "ledger.tsv")
        assert sorted(f"{r['economy']} P{r['pillar']} {r['engine']}" for r in rows) == sorted(
            launcher.started
        )

    def test_the_largest_estimate_starts_first(self, db, tmp_path):
        """SG P7 0.087, SG P6 0.075, MY P7 0.057, MY P6 0.046 on engine-b."""
        launcher = FakeLauncher()
        pre_run.main(
            args_for(db, tmp_path, "--economies", "MY,SG", "--pillars", "6,7",
                     "--engines", "engine-b", "--ceiling-usd", "35", "--paid",
                     "--parallel", "2"),
            launch=launcher,
        )
        assert launcher.started == [
            "SG P7 engine-b", "SG P6 engine-b", "MY P7 engine-b", "MY P6 engine-b",
        ]

    def test_each_run_gets_its_own_log_file_named_for_its_combination(
        self, db, tmp_path, capsys
    ):
        launcher = FakeLauncher()
        pre_run.main(
            args_for(db, tmp_path, "--economies", "SG", "--pillars", "6,7",
                     "--engines", "fake", "--ceiling-usd", "1", "--parallel", "2",
                     "--log-dir", str(tmp_path / "logs")),
            launch=launcher,
        )
        assert launcher.log_paths == {
            "SG P6 fake": tmp_path / "logs" / "SG-P6-fake.log",
            "SG P7 fake": tmp_path / "logs" / "SG-P7-fake.log",
        }
        out = capsys.readouterr().out
        for name, path in launcher.log_paths.items():
            assert f"start {name}" in out
            assert str(path) in out

    def test_the_log_folder_defaults_to_beside_the_ledger(self, db, tmp_path):
        launcher = FakeLauncher()
        pre_run.main(
            args_for(db, tmp_path, "--economies", "SG", "--pillars", "7",
                     "--engines", "fake", "--ceiling-usd", "1", "--parallel", "2"),
            launch=launcher,
        )
        assert launcher.log_paths["SG P7 fake"].parent.parent == tmp_path

    def test_every_finish_prints_the_runs_numbers(self, db, tmp_path, capsys):
        launcher = FakeLauncher(costs={("SG", 7, "engine-b"): 0.05})
        pre_run.main(
            args_for(db, tmp_path, "--economies", "SG", "--pillars", "7",
                     "--engines", "engine-b", "--ceiling-usd", "35", "--paid",
                     "--parallel", "2"),
            launch=launcher,
        )
        out = capsys.readouterr().out
        assert "done  SG P7 engine-b" in out
        assert "  ledger: " in out
        assert (
            "  SG P7 engine-b: 3 Documents, 7 Mappings, 11 Engine calls, 120.0 s,"
            " USD 0.0500 (USD 0.0500 of 35.00 spent)"
        ) in out

    def test_a_completed_combination_is_still_skipped(self, db, tmp_path):
        write_run(db, "SG", 6, "fake", run_id="run_done")
        launcher = FakeLauncher()
        pre_run.main(
            args_for(db, tmp_path, "--economies", "SG", "--pillars", "6,7",
                     "--engines", "fake", "--ceiling-usd", "1", "--parallel", "2"),
            launch=launcher,
        )
        assert launcher.started == ["SG P7 fake"]


# ---------------------------------------------------------------------------
# the money guard counts what is still running
# ---------------------------------------------------------------------------


class TestTheCeilingCountsRunsInFlight:
    ENGINE_B = ("--engines", "engine-b", "--paid", "--parallel", "3")

    def test_a_run_that_would_cross_it_with_the_runs_in_flight_is_not_started(
        self, db, tmp_path, capsys
    ):
        """0.087 + 0.075 in flight + 0.057 = 0.219 > 0.20, so MY P7 waits.
        The two finish at USD 0.01 each, and then everything fits."""
        costs = {
            ("SG", 7, "engine-b"): 0.01, ("SG", 6, "engine-b"): 0.01,
            ("MY", 7, "engine-b"): 0.01, ("MY", 6, "engine-b"): 0.01,
        }
        launcher = FakeLauncher(costs=costs)
        code = pre_run.main(
            args_for(db, tmp_path, "--economies", "SG,MY", "--pillars", "6,7",
                     "--ceiling-usd", "0.20", *self.ENGINE_B),
            launch=launcher,
        )
        out = capsys.readouterr().out
        assert code == 0
        assert launcher.running_at_start["SG P6 engine-b"] == {"SG P7 engine-b"}
        # MY P7 was held while both SG Runs were in flight.
        assert launcher.running_at_start["MY P7 engine-b"] == set()
        assert "hold  MY P7 engine-b" in out
        assert "in flight" in out
        assert len(launcher.started) == 4

    def test_a_run_that_cannot_fit_stops_the_job_after_the_others_finish(
        self, db, tmp_path, capsys
    ):
        """Ceiling 0.17 and every Run costs its estimate: SG P7 and SG P6 fit
        (0.162), MY P7 never does. It is never started, both SG Runs are
        recorded, and the job exits 3."""
        costs = {("SG", 7, "engine-b"): 0.087, ("SG", 6, "engine-b"): 0.075}
        launcher = FakeLauncher(costs=costs)
        code = pre_run.main(
            args_for(db, tmp_path, "--economies", "SG,MY", "--pillars", "6,7",
                     "--ceiling-usd", "0.17", *self.ENGINE_B),
            launch=launcher,
        )
        out = capsys.readouterr().out
        assert code == 3
        assert launcher.started == ["SG P7 engine-b", "SG P6 engine-b"]
        assert "STOP before MY P7 engine-b" in out
        assert "ceiling of USD 0.17" in out
        rows = pre_run.read_ledger(tmp_path / "ledger.tsv")
        assert [(r["economy"], r["pillar"]) for r in rows] == [("SG", 7), ("SG", 6)]

    def test_the_ledger_from_earlier_jobs_counts_too(self, db, tmp_path, capsys):
        pre_run.append_ledger(
            tmp_path / "ledger.tsv",
            {
                "timestamp": "2026-09-15T00:00:00Z", "economy": "AU", "pillar": 6,
                "engine": "engine-b", "run_id": "run_before", "documents": 1,
                "mappings": 1, "wall_s": 1.0, "engine_calls": 1, "usd": 0.15,
            },
        )
        launcher = FakeLauncher()
        code = pre_run.main(
            args_for(db, tmp_path, "--economies", "SG", "--pillars", "6,7",
                     "--ceiling-usd", "0.20", *self.ENGINE_B),
            launch=launcher,
        )
        assert code == 3
        assert launcher.started == []
        assert "STOP before SG P7 engine-b" in capsys.readouterr().out

    def test_a_single_expensive_run_stops_new_starts_when_it_finishes(
        self, db, tmp_path, capsys
    ):
        launcher = FakeLauncher(
            costs={("SG", 7, "engine-b"): 4.0},
            polls={("SG", 7, "engine-b"): 1, ("SG", 6, "engine-b"): 3},
        )
        code = pre_run.main(
            args_for(db, tmp_path, "--economies", "SG,MY", "--pillars", "6,7",
                     "--ceiling-usd", "35", "--max-run-usd", "3", "--paid",
                     "--engines", "engine-b", "--parallel", "2"),
            launch=launcher,
        )
        out = capsys.readouterr().out
        assert code == 3
        assert launcher.started == ["SG P7 engine-b", "SG P6 engine-b"]
        assert "STOP after SG P7 engine-b" in out
        rows = pre_run.read_ledger(tmp_path / "ledger.tsv")
        assert {(r["economy"], r["pillar"]) for r in rows} == {("SG", 7), ("SG", 6)}


# ---------------------------------------------------------------------------
# a failed Run
# ---------------------------------------------------------------------------


class TestAFailedRunStopsNewStarts:
    def test_in_flight_runs_finish_and_record_and_the_job_exits_1(
        self, db, tmp_path, capsys
    ):
        launcher = FakeLauncher(
            fail={("SG", 6, "fake"): 1},
            polls={("SG", 6, "fake"): 1, ("SG", 7, "fake"): 4},
        )
        code = pre_run.main(
            args_for(db, tmp_path, "--economies", "SG,MY", "--pillars", "6,7",
                     "--engines", "fake", "--ceiling-usd", "1", "--parallel", "2"),
            launch=launcher,
        )
        out = capsys.readouterr().out
        assert code == 1
        assert launcher.started == ["SG P6 fake", "SG P7 fake"]
        assert launcher.finished == ["SG P6 fake", "SG P7 fake"]
        assert not any(p.terminated for p in launcher.processes)
        assert "STOP: SG P6 fake exited 1" in out
        assert "SG-P6-fake.log" in out
        rows = pre_run.read_ledger(tmp_path / "ledger.tsv")
        assert [(r["economy"], r["pillar"]) for r in rows] == [("SG", 7)]

    def test_a_run_that_leaves_no_record_is_a_failure(self, db, tmp_path, capsys):
        launcher = FakeLauncher(no_record={("SG", 6, "fake")})
        code = pre_run.main(
            args_for(db, tmp_path, "--economies", "SG", "--pillars", "6,7",
                     "--engines", "fake", "--ceiling-usd", "1", "--parallel", "2"),
            launch=launcher,
        )
        assert code == 1
        assert "SG P6 fake exited 0 but left no completed Run Record" in (
            capsys.readouterr().out
        )

    def test_a_failure_outranks_a_money_stop(self, db, tmp_path):
        launcher = FakeLauncher(
            costs={("SG", 7, "engine-b"): 4.0},
            fail={("SG", 6, "engine-b"): 1},
            polls={("SG", 7, "engine-b"): 1, ("SG", 6, "engine-b"): 2},
        )
        code = pre_run.main(
            args_for(db, tmp_path, "--economies", "SG", "--pillars", "6,7",
                     "--ceiling-usd", "35", "--max-run-usd", "3", "--paid",
                     "--engines", "engine-b", "--parallel", "2"),
            launch=launcher,
        )
        assert code == 1


# ---------------------------------------------------------------------------
# an interrupted job
# ---------------------------------------------------------------------------


class TestAnInterruptedJob:
    def test_the_children_are_terminated_and_it_says_so(
        self, db, tmp_path, capsys, monkeypatch
    ):
        def interrupted(seconds):
            raise KeyboardInterrupt

        monkeypatch.setattr(pre_run, "_sleep", interrupted)
        launcher = FakeLauncher(default_polls=5)
        code = pre_run.main(
            args_for(db, tmp_path, "--economies", "SG,MY", "--pillars", "7",
                     "--engines", "fake", "--ceiling-usd", "1", "--parallel", "2"),
            launch=launcher,
        )
        out = capsys.readouterr().out
        assert code == 130
        assert len(launcher.processes) == 2
        assert all(p.terminated for p in launcher.processes)
        assert "INTERRUPTED" in out
        assert "SG P7 fake" in out and "MY P7 fake" in out


class TestAnInterruptAfterARunFinished:
    def test_a_run_that_exited_0_in_the_last_gap_is_recorded_not_terminated(
        self, db, tmp_path, capsys, monkeypatch
    ):
        """The interrupt lands before the parent polled again, and one Run had
        already finished in that gap. Its Run Record is complete, so it is
        ledgered (a resumed job will skip it, so the ledger is the only place
        its cost can land); only the Run still going is terminated."""
        def interrupted(seconds):
            raise KeyboardInterrupt

        monkeypatch.setattr(pre_run, "_sleep", interrupted)
        launcher = FakeLauncher(
            costs={("SG", 7, "engine-b"): 0.05},
            polls={("SG", 7, "engine-b"): 1, ("SG", 6, "engine-b"): 5},
        )
        code = pre_run.main(
            args_for(db, tmp_path, "--economies", "SG", "--pillars", "6,7",
                     "--engines", "engine-b", "--paid", "--ceiling-usd", "35",
                     "--parallel", "2"),
            launch=launcher,
        )
        out = capsys.readouterr().out
        assert code == 130
        finished, going = launcher.processes
        assert str(finished.combo) == "SG P7 engine-b"
        assert not finished.terminated
        assert going.terminated
        rows = pre_run.read_ledger(tmp_path / "ledger.tsv")
        assert [(r["economy"], r["pillar"], r["usd"]) for r in rows] == [("SG", 7, 0.05)]
        assert "SG P7 engine-b had already finished" in out
        assert "terminated SG P6 engine-b" in out
        unrecorded = [line for line in out.splitlines() if "not in the ledger" in line]
        assert len(unrecorded) == 1
        assert "SG P6 engine-b" in unrecorded[0]
        assert "SG P7 engine-b" not in unrecorded[0]


class TestAnErrorInTheJobItself:
    def test_a_launch_that_fails_stops_the_runs_already_going(
        self, db, tmp_path, capsys
    ):
        launcher = FakeLauncher(raise_on_call=2, default_polls=5)
        code = pre_run.main(
            args_for(db, tmp_path, "--economies", "SG", "--pillars", "6,7",
                     "--engines", "fake", "--ceiling-usd", "1", "--parallel", "2"),
            launch=launcher,
        )
        out = capsys.readouterr().out
        assert code == 1
        assert len(launcher.processes) == 1
        assert launcher.processes[0].terminated
        assert "ERROR" in out
        assert "could not start regcompass run" in out
        assert "terminated SG P6 fake" in out
        assert "SG-P6-fake.log" in out

    def test_a_ledger_that_cannot_be_written_stops_the_runs_still_going(
        self, db, tmp_path, capsys, monkeypatch
    ):
        def broken(path, row):
            raise OSError("disk full")

        monkeypatch.setattr(pre_run, "append_ledger", broken)
        launcher = FakeLauncher(polls={("SG", 6, "fake"): 1, ("SG", 7, "fake"): 5})
        code = pre_run.main(
            args_for(db, tmp_path, "--economies", "SG", "--pillars", "6,7",
                     "--engines", "fake", "--ceiling-usd", "1", "--parallel", "2"),
            launch=launcher,
        )
        out = capsys.readouterr().out
        assert code == 1
        assert "disk full" in out
        assert [p.terminated for p in launcher.processes] == [False, True]
        assert "terminated SG P7 fake" in out

    def test_the_read_only_connection_waits_for_the_lock(self, db):
        conn = pre_run._connect(db)
        try:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 60_000
        finally:
            conn.close()


class TestWhatAParallelJobSaysWhileItRuns:
    def test_the_waiting_line_is_printed_once_per_count(self, db, tmp_path, capsys):
        launcher = FakeLauncher(
            fail={("SG", 6, "fake"): 1},
            polls={("SG", 6, "fake"): 1, ("SG", 7, "fake"): 6},
        )
        pre_run.main(
            args_for(db, tmp_path, "--economies", "SG", "--pillars", "6,7",
                     "--engines", "fake", "--ceiling-usd", "1", "--parallel", "2"),
            launch=launcher,
        )
        out = capsys.readouterr().out
        assert out.count("waiting for 1 Run(s) in flight") == 1

    def test_a_heartbeat_names_each_run_its_minutes_and_its_last_log_line(
        self, db, tmp_path, capsys, monkeypatch
    ):
        ticks = iter(range(0, 10_000, 40))
        monkeypatch.setattr(pre_run, "_clock", lambda: float(next(ticks)))
        monkeypatch.setattr(pre_run, "HEARTBEAT_S", 100)
        launcher = FakeLauncher(
            default_polls=6,
            log_text={
                ("SG", 7, "fake"): "first line\n\x1b[1;32mM6 map | pair 12/40\x1b[0m\n\n",
            },
        )
        pre_run.main(
            args_for(db, tmp_path, "--economies", "SG", "--pillars", "7",
                     "--engines", "fake", "--ceiling-usd", "1", "--parallel", "2"),
            launch=launcher,
        )
        beats = [
            line for line in capsys.readouterr().out.splitlines()
            if line.startswith("alive SG P7 fake")
        ]
        assert beats
        assert "min" in beats[0]
        assert beats[0].endswith("M6 map | pair 12/40")
        assert "" not in beats[0]

    def test_a_run_with_no_log_yet_says_so(self, db, tmp_path, capsys, monkeypatch):
        ticks = iter(range(0, 10_000, 40))
        monkeypatch.setattr(pre_run, "_clock", lambda: float(next(ticks)))
        monkeypatch.setattr(pre_run, "HEARTBEAT_S", 100)
        pre_run.main(
            args_for(db, tmp_path, "--economies", "SG", "--pillars", "7",
                     "--engines", "fake", "--ceiling-usd", "1", "--parallel", "2"),
            launch=FakeLauncher(default_polls=6),
        )
        out = capsys.readouterr().out
        assert "alive SG P7 fake" in out
        assert "no output yet" in out

    def test_a_finish_names_dropped_and_skipped_pairs_and_retries(
        self, db, tmp_path, capsys
    ):
        details = {
            "documents": ["a", "b"], "n_passed": 5, "model_calls": 30,
            "n_dropped": 4, "rate_limit_retries": 3,
            "notes": [
                "c1::6.1: rate limited 4 times; pair skipped",
                "c2::6.2: transport failure persisted (TimeoutError); pair skipped",
                "c3::6.3: some other note",
            ],
        }
        launcher = FakeLauncher(details={("SG", 7, "fake"): details})
        pre_run.main(
            args_for(db, tmp_path, "--economies", "SG", "--pillars", "7",
                     "--engines", "fake", "--ceiling-usd", "1", "--parallel", "2"),
            launch=launcher,
        )
        out = capsys.readouterr().out
        assert (
            "  SG P7 fake: 4 pairs dropped (2 skipped: 1 rate limited,"
            " 1 transport failure), 3 rate-limit retries"
        ) in out

    def test_a_clean_run_prints_no_extra_line(self, db, tmp_path, capsys):
        details = {"documents": ["a"], "n_passed": 5, "model_calls": 30,
                   "n_dropped": 0, "rate_limit_retries": 0, "notes": []}
        pre_run.main(
            args_for(db, tmp_path, "--economies", "SG", "--pillars", "7",
                     "--engines", "fake", "--ceiling-usd", "1", "--parallel", "2"),
            launch=FakeLauncher(details={("SG", 7, "fake"): details}),
        )
        out = capsys.readouterr().out
        assert "dropped" not in out
        assert "retries" not in out


# ---------------------------------------------------------------------------
# the real thing: two `regcompass run` processes on one database at once
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    """One scratch database holding the bundled SG and MY legislation."""
    from corpus_fixtures import seed_corpus

    root = tmp_path_factory.mktemp("seeded")
    storage = Storage(root / "work.db")
    storage.apply_schema()
    for economy in ("SG", "MY"):
        seed_corpus(storage, root / "data", economy)
    storage.close()
    return root


def _copy_of(seeded: Path, dest: Path) -> Path:
    shutil.copytree(seeded, dest)
    return dest


def _exports(root: Path) -> dict[tuple[str, int], bytes]:
    storage = Storage(root / "work.db")
    try:
        done = pre_run.completed_runs(root / "work.db")
        return {
            (economy, pillar): export_from_db(
                storage, root / "out" / f"{economy}{pillar}", run_id=record["run_id"]
            ).csv_path.read_bytes()
            for (economy, pillar, _), record in done.items()
        }
    finally:
        storage.close()


def _overlapping(root: Path) -> bool:
    """Whether at least two of the Runs recorded here were going at once."""
    spans = [
        (record["started_at"], record["ended_at"])
        for record in pre_run.completed_runs(root / "work.db").values()
    ]
    return any(
        a[0] < b[1] and b[0] < a[1]
        for i, a in enumerate(spans)
        for b in spans[i + 1:]
    )


def test_real_runs_at_once_on_one_database_match_runs_done_alone(
    seeded, tmp_path, capsys, monkeypatch
):
    """Four fake-Engine Runs (two Economies, two Pillars each, so two Runs of
    one Economy also overlap) start together against one database. All four
    complete, and each exports the same bytes as the same Run done alone on a
    copy of the same database."""
    import time

    monkeypatch.setattr(pre_run, "_sleep", time.sleep)
    monkeypatch.setattr(pre_run, "POLL_S", 0.1)
    together = _copy_of(seeded, tmp_path / "together")
    alone = _copy_of(seeded, tmp_path / "alone")
    plan = ("--economies", "SG,MY", "--pillars", "6,7", "--engines", "fake",
            "--ceiling-usd", "1", "--yes")

    code = pre_run.main([
        "--db", str(together / "work.db"), "--data-dir", str(together / "data"),
        "--ledger", str(together / "ledger.tsv"), "--log-dir", str(together / "logs"),
        "--parallel", "4", *plan,
    ])
    out = capsys.readouterr().out
    assert code == 0, out
    rows = pre_run.read_ledger(together / "ledger.tsv")
    assert len(rows) == 4
    assert _overlapping(together), "the four Runs never ran at the same time"
    for economy in ("SG", "MY"):
        for pillar in (6, 7):
            log = together / "logs" / f"{economy}-P{pillar}-fake.log"
            assert "records in" in log.read_text(encoding="utf-8")

    code = pre_run.main([
        "--db", str(alone / "work.db"), "--data-dir", str(alone / "data"),
        "--ledger", str(alone / "ledger.tsv"), *plan,
    ])
    assert code == 0, capsys.readouterr().out

    parallel_exports = _exports(together)
    solo_exports = _exports(alone)
    assert set(parallel_exports) == {("SG", 6), ("SG", 7), ("MY", 6), ("MY", 7)}
    for key, content in parallel_exports.items():
        assert content, key
        assert content == solo_exports[key], key


# ---------------------------------------------------------------------------
# a database that is not yet in write-ahead-log mode
# ---------------------------------------------------------------------------


def _rollback_journal(db: Path) -> Path:
    """Put the fixture database back in rollback-journal mode, the state a
    copied or freshly built working database arrives in."""
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.close()
    assert _journal_mode(db) == "delete"
    return db


def _journal_mode(db: Path) -> str:
    import sqlite3

    conn = sqlite3.connect(db)
    try:
        return conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()


class TestTheDatabaseIsSwitchedOnceBeforeAnyRunStarts:
    def test_it_is_in_wal_mode_when_the_first_run_starts(self, db, tmp_path):
        _rollback_journal(db)
        modes_at_launch: list[str] = []
        launcher = FakeLauncher()

        def launch(combo, **kwargs):
            modes_at_launch.append(_journal_mode(kwargs["db"]))
            return launcher(combo, **kwargs)

        code = pre_run.main(
            args_for(db, tmp_path, "--economies", "SG,MY", "--pillars", "6,7",
                     "--engines", "fake", "--ceiling-usd", "1", "--parallel", "4"),
            launch=launch,
        )
        assert code == 0
        assert len(modes_at_launch) == 4
        assert modes_at_launch == ["wal"] * 4

    def test_a_dry_run_leaves_the_mode_alone(self, db, tmp_path, capsys):
        _rollback_journal(db)
        code = pre_run.main(
            args_for(db, tmp_path, "--economies", "SG,MY", "--pillars", "6,7",
                     "--engines", "fake", "--ceiling-usd", "1", "--parallel", "4",
                     "--dry-run"),
            launch=no_launch,
        )
        assert code == 0
        assert "nothing was run" in capsys.readouterr().out
        assert _journal_mode(db) == "delete"
