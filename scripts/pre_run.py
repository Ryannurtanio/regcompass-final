"""Pre-run the Prepared Economies on both Engines, resumably.

The job this script exists for: fill one database with real Run Records for
every (Economy, Pillar, Engine) combination that ships in the image, so a judge
who opens the interface with no key sees real Mappings, Comparisons and
exports. It spends money, so it is built to be interrupted and restarted and to
stop itself before it spends more than it was allowed.

Four properties matter more than anything else here.

1. It runs the SAME code path a reviewer runs. Every combination goes through
   `regcompass run` (the console script in this venv, or `regcompass.cli:app`
   when there is none), one Pillar and one Engine per Run, so nothing about the
   Run Record, the audit trail or the export depends on this file.
2. It is resumable. A combination that already has a completed, un-narrowed Run
   Record in the target database is skipped, so a killed job is restarted by
   typing the same command again. Nothing is repaid.
3. It stops on money, twice. `--ceiling-usd` is the total the operator
   authorised: before each Run the ledger total plus that combination's measured
   estimate is checked against it, and the job stops with the numbers
   printed rather than starting a Run that would cross it. `--max-run-usd`
   catches the other failure, a single Run that costs far more than its
   estimate; the Run is finished and recorded, and then the job stops.
4. Every Run lands in a ledger line as soon as it finishes, so a crash never
   loses the record of what was already paid for.

`--parallel N` keeps up to N Runs going at once, largest estimate first. Each
Run is its own `regcompass run` process writing one shared database (the
storage layer opens it in write-ahead-log mode with a long busy timeout, which
is what makes that safe), and each writes its narration to its own log file,
`<log dir>/<Economy>-P<n>-<engine>.log`, because several narrations on one
terminal read as noise. This process is the only one that writes the ledger,
and it prints one line when a Run starts (with its log) and one when it
finishes (with its numbers). The money guard counts the estimates of the Runs
still in flight: a Run that fits once they finish waits for them, a Run that
cannot fit even then stops the job. A failed Run or a money stop starts
nothing new; the Runs already going are left to finish and are recorded, and
then the job exits. Ctrl-C (or SIGTERM) terminates the Runs in flight and says
which. `--parallel 1`, the default, is the one-at-a-time job, unchanged.

Nothing here invents a paid-mode switch. The one thing the code requires for a
paid call is the Engine's key, checked by `preflight_key`
(`src/regcompass/engines.py:246`) inside the CLI before the database is opened;
`--paid` on this script is the operator's own confirmation that the spend is
intended, and its absence refuses a hosted Engine before anything runs.
REGCOMPASS_PAID is the pytest opt-in switch (`tests/conftest.py:43`), not a
runtime flag, and this script never sets it or any other environment variable.

Usage:

  uv run python scripts/pre_run.py --ceiling-usd 35 --paid
  uv run python scripts/pre_run.py --ceiling-usd 1 --engines fake --pillars 7 \
      --economies SG --db /tmp/scratch.db --data-dir /tmp/root --yes
  uv run python scripts/pre_run.py --ceiling-usd 35 --paid --yes --parallel 4
  uv run python scripts/pre_run.py --report

Exit codes: 0 the plan finished (or everything was already done), 1 a Run
failed, 2 a refusal before any Run (unknown Engine, hosted Engine without
--paid, no typed confirmation), 3 the job stopped itself on a money guard,
130 the job was interrupted (parallel only; the Runs in flight were
terminated). When a parallel job meets both a failure and a money stop, the
failure's 1 wins.

The per-combination estimates are the measured pre-run figures (the "real"
columns of the cost measurement, the fake-Gate bound corrected by 0.81). They are an input to
the ceiling guard only. What is REPORTED is always the Run Record's own
cost_usd, never an estimate.
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import sqlite3
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from regcompass.config import indicator_ids, load_default_pillars, load_portals
from regcompass.engines import UnknownEngine, resolve_engine
from regcompass.paths import resolve_stored_path
from regcompass.storage import Storage

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_DB = Path("data/regcompass.db")
DEFAULT_DATA_DIR = Path("data")
DEFAULT_LEDGER = Path("data/pre_run_ledger.tsv")
DEFAULT_ENGINES = ("engine-a", "engine-b")
DEFAULT_MAX_RUN_USD = 3.0
DEFAULT_LOG_DIR_NAME = "pre_run_logs"

# How often a parallel job looks at its Runs, and how long a terminated Run
# gets to exit before it is killed. Module attributes so a test can replace
# the sleep and never wait.
POLL_S = 2.0
TERMINATE_WAIT_S = 30.0
_sleep = time.sleep
_clock = time.monotonic

# How often a parallel job prints one line per Run still going, so a Run that
# hangs cannot sit silent: its minutes and the last thing its log said.
HEARTBEAT_S = 600.0

# The read-only connections this script opens wait this long for a Run's
# write lock rather than fail after SQLite's default five seconds.
READ_TIMEOUT_S = 120.0

LEDGER_COLUMNS = (
    "timestamp",
    "economy",
    "pillar",
    "engine",
    "run_id",
    "documents",
    "mappings",
    "wall_s",
    "engine_calls",
    "usd",
)

# The cost measurement's "real" USD columns: what one (Economy, Pillar) Run
# is expected to cost on each Engine. Measured on the Round 1 Corpus, so they
# describe AU, MY and SG only.
MEASURED_USD: dict[tuple[str, int], dict[str, float]] = {
    ("AU", 6): {"engine-a": 0.962, "engine-b": 0.131},
    ("AU", 7): {"engine-a": 1.269, "engine-b": 0.181},
    ("MY", 6): {"engine-a": 0.385, "engine-b": 0.046},
    ("MY", 7): {"engine-a": 0.473, "engine-b": 0.057},
    ("SG", 6): {"engine-a": 0.541, "engine-b": 0.075},
    ("SG", 7): {"engine-a": 0.646, "engine-b": 0.087},
}

# The same measurement per INDICATOR, for a Pillar that was not measured. Each
# Economy is scaled on its own yield (same measurement), because Malaysia's
# scanned Documents gate at less than half Australia's rate and a shared
# average would under-budget Australia and over-budget Malaysia.
MEASURED_PER_INDICATOR_USD: dict[str, dict[str, float]] = {
    "AU": {"engine-a": (0.962 + 1.269) / 9, "engine-b": (0.131 + 0.181) / 9},
    "MY": {"engine-a": (0.385 + 0.473) / 9, "engine-b": (0.046 + 0.057) / 9},
    "SG": {"engine-a": (0.541 + 0.646) / 9, "engine-b": (0.075 + 0.087) / 9},
}

# For an Economy with no measurement at all (Indonesia, Lao PDR, Thailand):
# the measured per-Document rate, which is the Gate's cap and so an
# upper bound. USD per Document per Indicator.
UNMEASURED_USD_PER_DOC_PER_INDICATOR = {"priced": 0.052 / 4, "open_weights": 0.007 / 4}

# What an Economy with no Corpus in the database is assumed to hold, so the
# estimate is a number rather than a zero. Singapore-sized, as the cost
# extrapolation assumes.
ASSUMED_DOCUMENTS = 10


@dataclass(frozen=True)
class Combo:
    """One unit of the matrix: this Economy, this Pillar, this Engine."""

    economy: str
    pillar: int
    engine: str

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.economy, self.pillar, self.engine)

    def __str__(self) -> str:
        return f"{self.economy} P{self.pillar} {self.engine}"


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------


def prepared_economies(config_dir: Path | None = None) -> tuple[str, ...]:
    """Every Economy portals.yaml marks `prepared: true`, in the order the file
    lists them. `prepared` means the Corpus has actually been fetched and
    checked, which is exactly the precondition a Run has."""
    return tuple(
        code for code, portal in load_portals(config_dir).items() if portal.prepared
    )


def plan_combos(
    economies: tuple[str, ...], pillars: tuple[int, ...], engines: tuple[str, ...]
) -> list[Combo]:
    """The matrix in a fixed order: Economy, then Pillar ascending, then Engine
    in the order they were named. Fixed because a resumed job must walk the
    same list as the job it resumes, and because an operator watching it should
    be able to say what runs next."""
    return [
        Combo(economy, pillar, engine)
        for economy in economies
        for pillar in sorted(pillars)
        for engine in engines
    ]


# ---------------------------------------------------------------------------
# the database: what is already done
# ---------------------------------------------------------------------------


def _connect(db: Path) -> sqlite3.Connection | None:
    """A read-only connection to an EXISTING database, or None. Never creates
    the file: pointing the job at the wrong path must say so, not leave an
    empty database behind. The runs table may be absent (a Round 1 database
    predates Run Records), so callers check for their table themselves."""
    if not Path(db).exists():
        return None
    conn = sqlite3.connect(
        f"file:{Path(db)}?mode=ro", uri=True, timeout=READ_TIMEOUT_S
    )
    conn.execute(f"PRAGMA busy_timeout = {int(READ_TIMEOUT_S * 1000)}")
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def completed_runs(db: Path) -> dict[tuple[str, int, str], dict]:
    """The newest completed Run Record for each (Economy, Pillar, Engine).

    Only a Run over a single Pillar with no Indicator narrowing counts: a Run
    narrowed to two Indicators (the sealed live test) answers a different
    question and must not make the job think the combination is covered.
    """
    conn = _connect(db)
    if conn is None:
        return {}
    out: dict[tuple[str, int, str], dict] = {}
    try:
        if not _has_table(conn, "runs"):
            return {}
        rows = conn.execute(
            "SELECT * FROM runs WHERE kind = 'run' AND status = 'completed'"
            " ORDER BY started_at ASC, rowid ASC"
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        record = dict(row)
        if record.get("indicators"):
            continue
        try:
            pillars = json.loads(record["pillars"] or "[]")
        except json.JSONDecodeError:  # pragma: no cover - defensive
            continue
        if len(pillars) != 1 or not record.get("engine"):
            continue
        record["pillars"] = [int(p) for p in pillars]
        record["details"] = _details(record.get("details"))
        out[(record["economy"], int(pillars[0]), record["engine"])] = record
    return out


def _details(raw) -> dict:
    if isinstance(raw, (str, bytes)):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:  # pragma: no cover - defensive
            return {}
    return raw or {}


@dataclass(frozen=True)
class MissingFile:
    """One Corpus row whose bytes are not under the data folder."""

    economy: str
    document_id: str
    stored: str

    def __str__(self) -> str:
        return f"{self.economy}  {self.document_id}  {self.stored}"


def missing_corpus_files(
    db: Path, economies: tuple[str, ...] | list[str], data_dir: Path
) -> list[MissingFile]:
    """Every Corpus row of these Economies whose file cannot be opened under
    `data_dir`, in Economy then id order.

    This is the check that would have caught a database shipping one machine's
    absolute paths before the image was built rather than in the middle of a
    paid job. It reads the same resolver a Run reads, so a row the Run would
    repair for itself is not reported; only a row with no bytes anywhere is.
    Reads nothing but the database and the file system, and spends nothing.
    """
    conn = _connect(db)
    if conn is None:
        return []
    try:
        if not _has_table(conn, "documents"):
            return []
        rows = conn.execute(
            "SELECT document_id, economy, local_path, source_sha256 FROM documents"
            " WHERE local_path IS NOT NULL ORDER BY economy, document_id"
        ).fetchall()
    finally:
        conn.close()
    wanted = set(economies)
    missing: list[MissingFile] = []
    for row in rows:
        if row["economy"] not in wanted:
            continue
        found = resolve_stored_path(
            row["local_path"], data_dir,
            economy=row["economy"], sha256=row["source_sha256"],
        )
        if found is None:
            missing.append(
                MissingFile(row["economy"], row["document_id"], str(row["local_path"]))
            )
    return missing


def document_count(db: Path, economy: str) -> int:
    """How many Documents this Economy's Corpus holds, 0 when the database has
    none. Only an input to the estimate for an unmeasured Economy."""
    conn = _connect(db)
    if conn is None:
        return 0
    try:
        if not _has_table(conn, "documents"):
            return 0
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM documents WHERE economy = ?", (economy,)
        ).fetchone()
        return int(row["n"] if row is not None else 0)
    finally:
        conn.close()


def duration_s(started_at: str | None, ended_at: str | None) -> float:
    """Seconds between two Run Record timestamps, 0.0 when either is missing."""
    if not started_at or not ended_at:
        return 0.0
    try:
        start = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(ended_at).replace("Z", "+00:00"))
    except ValueError:  # pragma: no cover - defensive
        return 0.0
    return max(0.0, (end - start).total_seconds())


# ---------------------------------------------------------------------------
# the estimate (the ceiling guard's only input)
# ---------------------------------------------------------------------------


def estimate_usd(
    combo: Combo,
    *,
    api_key_env: str | None,
    open_weights: bool,
    documents: int = 0,
    config_dir: Path | None = None,
) -> float:
    """What the cost measurement expects this combination to cost. A keyless Engine (the
    fake one, a locally served one) costs nothing by construction, so it is
    zero rather than a guess."""
    if not api_key_env:
        return 0.0
    measured = MEASURED_USD.get((combo.economy, combo.pillar), {}).get(combo.engine)
    if measured is not None:
        return measured
    n_indicators = len(indicator_ids((combo.pillar,), config_dir))
    per_indicator = MEASURED_PER_INDICATOR_USD.get(combo.economy, {}).get(combo.engine)
    if per_indicator is not None:
        return per_indicator * n_indicators
    rate = UNMEASURED_USD_PER_DOC_PER_INDICATOR[
        "open_weights" if open_weights else "priced"
    ]
    return rate * n_indicators * (documents or ASSUMED_DOCUMENTS)


# ---------------------------------------------------------------------------
# the ledger
# ---------------------------------------------------------------------------


def read_ledger(path: Path) -> list[dict]:
    """Every ledger line as a dict. The ledger is the authority on what was
    spent: it is appended after each Run, so it survives a crash that the
    in-memory total would not."""
    path = Path(path)
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if fields[0] == LEDGER_COLUMNS[0]:  # the header
            continue
        if len(fields) != len(LEDGER_COLUMNS):
            continue
        row = dict(zip(LEDGER_COLUMNS, fields))
        row["pillar"] = int(row["pillar"])
        for name in ("documents", "mappings", "engine_calls"):
            row[name] = int(row[name])
        for name in ("wall_s", "usd"):
            row[name] = float(row[name])
        rows.append(row)
    return rows


def append_ledger(path: Path, row: dict) -> str:
    """Append one Run to the ledger and return the line written. The header is
    written once, when the file is created."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = "\t".join(str(row[name]) for name in LEDGER_COLUMNS)
    new = not path.exists()
    with path.open("a", encoding="utf-8") as f:
        if new:
            f.write("\t".join(LEDGER_COLUMNS) + "\n")
        f.write(line + "\n")
    return line


def ledger_row(combo: Combo, record: dict) -> dict:
    """One ledger line from one Run Record. Every figure is the Run's own: the
    Documents and Mappings it counted, the wall time between its timestamps,
    the calls its meter saw and the cost its Engine's declared prices give."""
    details = _details(record.get("details"))
    return {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "economy": combo.economy,
        "pillar": combo.pillar,
        "engine": combo.engine,
        "run_id": record.get("run_id", ""),
        "documents": len(details.get("documents") or []),
        "mappings": int(details.get("n_passed") or 0),
        "wall_s": round(duration_s(record.get("started_at"), record.get("ended_at")), 1),
        "engine_calls": int(details.get("model_calls") or 0),
        "usd": round(float(record.get("cost_usd") or 0.0), 6),
    }


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------


def render_report(rows: list[dict]) -> str:
    """The coverage table the pre-run job reports, in Markdown: Economy by Pillar by
    Engine with Documents, Mappings, USD and minutes. Where a combination was
    run twice the LATEST line is shown (the database holds that Run's records),
    while the total line counts every line, because every line was paid for."""
    if not rows:
        return "No Runs in the ledger yet."
    latest: dict[tuple[str, int, str], dict] = {}
    for row in rows:
        latest[(row["economy"], row["pillar"], row["engine"])] = row
    shown = [latest[key] for key in sorted(latest)]
    out = [
        "| Economy | Pillar | Engine | Documents | Mappings | USD | Minutes |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in shown:
        out.append(
            f"| {row['economy']} | {row['pillar']} | {row['engine']} |"
            f" {row['documents']} | {row['mappings']} | {row['usd']:.4f} |"
            f" {row['wall_s'] / 60:.1f} |"
        )
    out.append(
        f"| **Total** | | | | {sum(r['mappings'] for r in shown)} |"
        f" {sum(r['usd'] for r in shown):.4f} |"
        f" {sum(r['wall_s'] for r in shown) / 60:.1f} |"
    )
    spent = sum(row["usd"] for row in rows)
    out.append("")
    out.append(
        f"{len(rows)} Run(s) in the ledger, USD {spent:.4f} spent in total"
        f" across {len(shown)} combination(s)."
    )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# running one combination
# ---------------------------------------------------------------------------


def cli_command(combo: Combo, *, db: Path, data_dir: Path, concurrency: int | None):
    """The exact `regcompass run` command line for one combination."""
    console = Path(sys.executable).with_name("regcompass")
    head = (
        [str(console)]
        if console.exists()
        else [sys.executable, "-c", "from regcompass.cli import app; app()"]
    )
    cmd = head + [
        "run",
        "--economy",
        combo.economy,
        "--pillar",
        str(combo.pillar),
        "--engine",
        combo.engine,
        "--db",
        str(db),
        "--data-dir",
        str(data_dir),
    ]
    if concurrency is not None:
        cmd += ["--concurrency", str(concurrency)]
    return cmd


def cli_execute(combo: Combo, *, db: Path, data_dir: Path, concurrency: int | None) -> int:
    """Run one combination through the CLI and return its exit code. Output is
    NOT captured: a Run takes minutes to hours and the operator watches its
    narration, which is the whole reason the CLI prints it."""
    cmd = cli_command(combo, db=db, data_dir=data_dir, concurrency=concurrency)
    print(f"  $ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=ROOT).returncode


def log_name(combo: Combo) -> str:
    """The log file one Run of a parallel job writes: `SG-P7-engine-a.log`."""
    return f"{combo.economy}-P{combo.pillar}-{combo.engine}.log"


def cli_launch(
    combo: Combo, *, db: Path, data_dir: Path, concurrency: int | None, log_path: Path
) -> subprocess.Popen:
    """Start one combination through the CLI WITHOUT waiting for it, its output
    appended to `log_path`, and hand back the process. The parallel job polls
    it; nothing else about the Run differs from `cli_execute`."""
    cmd = cli_command(combo, db=db, data_dir=data_dir, concurrency=concurrency)
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with log_path.open("ab") as log:
        log.write(f"\n=== {stamp} {combo}\n$ {' '.join(cmd)}\n".encode("utf-8"))
        log.flush()
        # The child holds its own copy of the file, so this one closes here.
        return subprocess.Popen(
            cmd, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT,
        )


# ---------------------------------------------------------------------------
# after a Run: the ledger line and the numbers
# ---------------------------------------------------------------------------


SEQUENTIAL_THEN = "nothing further was run."
PARALLEL_THEN = "no new Run starts."


def pair_losses(details: dict) -> str | None:
    """What a Run lost on the way, from its own Run Record: pairs dropped,
    how many of those were skipped (rate limited or a transport failure that
    outlived its retries, as the Run's notes name them) and the rate-limit
    retries it waited through. None when there is nothing to say. The record
    counts no transport retries, only transport skips, so none are shown."""
    dropped = int(details.get("n_dropped") or 0)
    retries = int(details.get("rate_limit_retries") or 0)
    skips = [n for n in details.get("notes") or [] if "pair skipped" in str(n)]
    rate = sum(1 for n in skips if "rate limited" in str(n))
    transport = sum(1 for n in skips if "transport" in str(n))
    other = len(skips) - rate - transport
    if not (dropped or retries or skips):
        return None
    parts = [f"{dropped} pairs dropped"]
    if skips:
        kinds = [
            f"{count} {name}"
            for count, name in (
                (rate, "rate limited"), (transport, "transport failure"),
                (other, "other"),
            )
            if count
        ]
        parts[0] += f" ({len(skips)} skipped: {', '.join(kinds)})"
    if retries:
        parts.append(f"{retries} rate-limit retries")
    return ", ".join(parts)


def _record_finished(
    combo: Combo, *, db: Path, ledger_path: Path, spent: float, args, then: str,
    losses: bool = False,
) -> tuple[float, int]:
    """What happens after a Run exits 0, in either mode: read its Run Record,
    append its ledger line, print its numbers, and apply `--max-run-usd`.
    Returns the new spent total and 0, or the stop code (1 no record, 3 over
    the per-Run limit). `then` finishes each STOP sentence."""
    record = completed_runs(db).get(combo.key)
    if record is None:
        print(
            f"STOP: {combo} exited 0 but left no completed Run Record in"
            f" {db}; {then}"
        )
        return spent, 1
    row = ledger_row(combo, record)
    line = append_ledger(ledger_path, row)
    spent += row["usd"]
    print(f"  ledger: {line}")
    print(
        f"  {combo}: {row['documents']} Documents, {row['mappings']} Mappings,"
        f" {row['engine_calls']} Engine calls, {row['wall_s']:.1f} s,"
        f" USD {row['usd']:.4f} (USD {spent:.4f} of {args.ceiling_usd:.2f} spent)",
        flush=True,
    )
    if losses:
        lost = pair_losses(_details(record.get("details")))
        if lost:
            print(f"  {combo}: {lost}", flush=True)
    if row["usd"] > args.max_run_usd:
        # The one-at-a-time job has always ended this sentence here; a
        # parallel job also says that nothing new starts.
        after = "" if then == SEQUENTIAL_THEN else f" {then[:1].upper()}{then[1:]}"
        print(
            f"STOP after {combo}: this single Run cost USD {row['usd']:.4f},"
            f" over --max-run-usd USD {args.max_run_usd:.2f}. Check the Run"
            f" Record before spending more.{after}"
        )
        return spent, 3
    return spent, 0


# ---------------------------------------------------------------------------
# several Runs at once
# ---------------------------------------------------------------------------


class _Terminated(Exception):
    """SIGTERM on the parent, turned into something the loop can catch."""


@dataclass
class _InFlight:
    """One Run a parallel job has started and not yet collected."""

    combo: Combo
    process: object
    log_path: Path
    started: float


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def last_log_line(path: Path, *, tail_bytes: int = 65536) -> str | None:
    """The last non-empty line a Run wrote to its log, colour codes removed,
    or None when there is no log or nothing in it yet. A progress bar redraws
    with carriage returns, so those count as line ends too."""
    try:
        with Path(path).open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(re.split(r"[\r\n]", _ANSI.sub("", text))):
        if line.strip():
            return line.strip()
    return None


def run_parallel(
    todo: list[Combo],
    estimates: dict[tuple[str, int, str], float],
    *,
    parallel: int,
    launch,
    db: Path,
    data_dir: Path,
    ledger_path: Path,
    log_dir: Path,
    spent: float,
    args,
) -> int:
    """Keep up to `parallel` Runs going until the plan is done or stopped, and
    return the exit code. This process is the only writer of the ledger.

    Whatever goes wrong in THIS process (an interrupt, a database it cannot
    read, a ledger it cannot write, a Run it cannot start), the Runs it
    started are not left running and spending unwatched: each one that has
    already finished is recorded, the rest are terminated, and the job says
    which."""
    # Put the database in write-ahead-log mode here, once, before any Run
    # starts. Each Run's first act is that same switch, and several Runs
    # making it on one file at the same moment is a race over its write lock;
    # a file already switched makes every Run's open a plain read.
    Storage(db).close()

    # Largest estimate first, so the longest Runs are not the ones left
    # running alone at the end. sorted() is stable: ties keep plan order.
    pending = sorted(todo, key=lambda combo: -estimates[combo.key])
    running: list[_InFlight] = []
    held: set[tuple[str, int, str]] = set()
    code = 0
    then = PARALLEL_THEN
    waiting_said = 0
    last_beat = _clock()

    def stop(new_code: int) -> None:
        nonlocal code
        # A failed Run outranks a money stop: it is the one a person must read.
        if code != 1:
            code = new_code

    def record(combo: Combo) -> None:
        nonlocal spent
        spent, result = _record_finished(
            combo, db=db, ledger_path=ledger_path, spent=spent, args=args,
            then=then, losses=True,
        )
        if result:
            stop(result)

    def on_sigterm(signum, frame):  # pragma: no cover - exercised by hand
        raise _Terminated()

    previous = None
    try:
        previous = signal.signal(signal.SIGTERM, on_sigterm)
    except ValueError:  # pragma: no cover - not the main thread
        previous = None

    try:
        while pending or running:
            # Start what fits.
            while code == 0 and pending and len(running) < parallel:
                combo = pending[0]
                estimate = estimates[combo.key]
                in_flight = sum(estimates[r.combo.key] for r in running)
                if spent + estimate > args.ceiling_usd:
                    print(
                        f"STOP before {combo}: USD {spent:.4f} spent plus an"
                        f" estimated USD {estimate:.4f} would pass the ceiling of"
                        f" USD {args.ceiling_usd:.2f}. Raise --ceiling-usd to"
                        " continue."
                        + (
                            f" {len(running)} Run(s) in flight will finish."
                            if running else ""
                        ),
                        flush=True,
                    )
                    stop(3)
                    break
                if spent + in_flight + estimate > args.ceiling_usd:
                    # It fits on what is spent, not on what may yet be spent:
                    # wait for a Run to finish and look again.
                    if combo.key not in held:
                        held.add(combo.key)
                        print(
                            f"hold  {combo}: USD {spent:.4f} spent plus USD"
                            f" {in_flight:.4f} in flight plus an estimated USD"
                            f" {estimate:.4f} would pass the ceiling of USD"
                            f" {args.ceiling_usd:.2f}; waiting for a Run to finish.",
                            flush=True,
                        )
                    break
                log_path = Path(log_dir) / log_name(combo)
                process = launch(
                    combo, db=db, data_dir=data_dir, concurrency=args.concurrency,
                    log_path=log_path,
                )
                pending.pop(0)
                running.append(_InFlight(combo, process, log_path, _clock()))
                print(
                    f"start {combo} (estimated USD {estimate:.4f}, {len(running)}"
                    f" of {parallel} running): log {log_path}",
                    flush=True,
                )
            if not running:
                break
            _sleep(POLL_S)
            # Collect what finished, in start order. A Run leaves `running`
            # only once it has been dealt with, so an error part way through
            # this loop still finds every unrecorded Run in the list.
            for run in list(running):
                exit_code = run.process.poll()
                if exit_code is None:
                    continue
                print(
                    f"done  {run.combo}: exited {exit_code}, log {run.log_path}",
                    flush=True,
                )
                if exit_code != 0:
                    running.remove(run)
                    print(
                        f"STOP: {run.combo} exited {exit_code} (see"
                        f" {run.log_path}); {then}",
                        flush=True,
                    )
                    stop(1)
                    continue
                record(run.combo)
                running.remove(run)
            now = _clock()
            if running and now - last_beat >= HEARTBEAT_S:
                last_beat = now
                for run in running:
                    said = last_log_line(run.log_path) or "(no output yet)"
                    print(
                        f"alive {run.combo}: {(now - run.started) / 60:.1f} min,"
                        f" last log line: {said}",
                        flush=True,
                    )
            if code and running and len(running) != waiting_said:
                waiting_said = len(running)
                print(
                    f"waiting for {len(running)} Run(s) in flight to finish:"
                    f" {', '.join(str(r.combo) for r in running)}",
                    flush=True,
                )
    except (KeyboardInterrupt, _Terminated):
        print(f"\nINTERRUPTED: stopping {len(running)} Run(s) in flight.", flush=True)
        _terminate(running, record)
        return 130
    except Exception as exc:  # noqa: BLE001 - every Run must be stopped first
        print(
            f"\nERROR in the pre-run job itself: {type(exc).__name__}: {exc}."
            f" Stopping {len(running)} Run(s) in flight so none keeps spending"
            " unwatched.",
            flush=True,
        )
        traceback.print_exc(file=sys.stdout)
        _terminate(running, record)
        return 1
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
    if code and pending:
        print(
            f"not started: {', '.join(str(c) for c in pending)}. Type the same"
            " command again to resume once the cause is fixed.",
            flush=True,
        )
    return code


def _terminate(running: list[_InFlight], record) -> None:
    """Stop every Run still going and say so. A Run that has already exited 0
    left a completed Run Record, which a resumed job skips, so it is recorded
    here (the ledger is the only place its cost can land). Every other Run is
    terminated; its record stays unfinished, so it is not coverage and a
    resumed job runs it again."""
    stopped: list[_InFlight] = []
    for run in running:
        if run.process.poll() is None:
            try:
                run.process.terminate()
            except OSError:  # pragma: no cover - already gone
                pass
    for run in running:
        try:
            exit_code = run.process.wait(timeout=TERMINATE_WAIT_S)
        except subprocess.TimeoutExpired:  # pragma: no cover - a stuck child
            run.process.kill()
            exit_code = run.process.wait()
        if exit_code == 0:
            print(
                f"  {run.combo} had already finished (exit 0); recording it."
                f" Log {run.log_path}",
                flush=True,
            )
            try:
                record(run.combo)
            except Exception as exc:  # noqa: BLE001 - keep stopping the rest
                print(
                    f"  could not record {run.combo}: {type(exc).__name__}: {exc}."
                    f" Its Run Record is complete; add its ledger line by hand.",
                    flush=True,
                )
            continue
        stopped.append(run)
        print(
            f"  terminated {run.combo} (exit {exit_code}, log {run.log_path})",
            flush=True,
        )
    if stopped:
        print(
            "Anything these Runs had already spent is not in the ledger (they"
            " left no completed Run Record):"
            f" {', '.join(str(r.combo) for r in stopped)}."
            " Type the same command again to resume.",
            flush=True,
        )


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------


def _codes(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pre_run.py",
        description=(
            "Pre-run every Prepared Economy on both Engines through"
            " `regcompass run`, resumably, under a hard USD ceiling."
        ),
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite working database")
    parser.add_argument(
        "--data-dir", type=Path, default=DEFAULT_DATA_DIR,
        help="Root the Corpus's stored bytes live under",
    )
    parser.add_argument(
        "--economies", default="",
        help="Comma-separated Economy codes (default: every `prepared: true`"
        " Economy in config/portals.yaml)",
    )
    parser.add_argument(
        "--pillars", default="",
        help="Comma-separated Pillar numbers (default: pillars.json"
        " default_pillars, which is 6,7)",
    )
    parser.add_argument(
        "--engines", default=",".join(DEFAULT_ENGINES),
        help="Comma-separated Engine names from config/models.yaml",
    )
    parser.add_argument(
        "--ceiling-usd", type=float, default=None,
        help="REQUIRED unless --report: the total spend authorised. The job"
        " stops before the Run that would cross it.",
    )
    parser.add_argument(
        "--max-run-usd", type=float, default=DEFAULT_MAX_RUN_USD,
        help="Stop after any single Run whose recorded cost exceeds this"
        f" (default {DEFAULT_MAX_RUN_USD})",
    )
    parser.add_argument(
        "--ledger", type=Path, default=DEFAULT_LEDGER,
        help=f"Append-only TSV of finished Runs (default {DEFAULT_LEDGER})",
    )
    parser.add_argument(
        "--paid", action="store_true",
        help="Confirm that Runs on a hosted Engine may bill the account. Sets"
        " nothing: the key the Engine needs still comes from .env, checked by"
        " the CLI's own preflight.",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Skip the typed confirmation (for an unattended job)",
    )
    parser.add_argument(
        "--report", action="store_true",
        help="Print the coverage table from the ledger and exit, running nothing",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the plan and the estimate, run nothing",
    )
    parser.add_argument(
        "--concurrency", type=int, default=None,
        help="Mapping calls to keep in flight, passed to `regcompass run`"
        " (default: the Engine's own value)",
    )
    parser.add_argument(
        "--parallel", type=int, default=1,
        help="Runs to keep going at once (default 1: one at a time). Above 1"
        " each Run's narration goes to its own log file in --log-dir.",
    )
    parser.add_argument(
        "--log-dir", type=Path, default=None,
        help="Where a parallel job writes one log per Run (default: a"
        f" {DEFAULT_LOG_DIR_NAME} folder beside the ledger)",
    )
    parser.add_argument(
        "--config-dir", type=Path, default=None, help=argparse.SUPPRESS
    )
    return parser


def _confirm(prompt: str) -> bool:
    """A typed yes, or nothing happens. A job with no terminal answering it
    refuses instead of blocking forever on a closed stdin."""
    if not sys.stdin or not sys.stdin.isatty():
        print(
            "stdin is not a terminal and --yes was not given: refusing to start"
            " without a confirmation."
        )
        return False
    try:
        return input(prompt).strip().lower() == "yes"
    except EOFError:  # pragma: no cover - defensive
        return False


def main(argv: list[str] | None = None, execute=cli_execute, launch=cli_launch) -> int:
    args = build_parser().parse_args(argv)
    ledger_path = Path(args.ledger)

    if args.report:
        print(render_report(read_ledger(ledger_path)))
        return 0

    if args.ceiling_usd is None:
        print("--ceiling-usd is required: name the total spend you authorise.")
        return 2
    if args.ceiling_usd <= 0:
        print("--ceiling-usd must be positive.")
        return 2
    if args.parallel < 1:
        print("--parallel must be 1 or more.")
        return 2

    db = Path(args.db).resolve()
    data_dir = Path(args.data_dir).resolve()

    engine_names = _codes(args.engines) or DEFAULT_ENGINES
    engines = {}
    for name in engine_names:
        try:
            engines[name] = resolve_engine(name)
        except UnknownEngine as e:
            print(str(e))
            return 2

    # The money guard, before anything else can run. --dry-run is exempt
    # because it runs nothing, and an operator sizing the job should be able to
    # see the plan before deciding to authorise it.
    hosted = [name for name, e in engines.items() if e.api_key_env]
    if hosted and not args.paid:
        message = (
            f"{', '.join(hosted)} bill real money (the key in .env is what the"
            " code requires). Re-run with --paid once the spend is approved,"
            " or pass --engines fake to rehearse the job for nothing."
        )
        if not args.dry_run:
            print(message)
            return 2
        print(f"NOTE: {message}")

    economies = _codes(args.economies) or prepared_economies(args.config_dir)
    if not economies:
        print("no Economy is marked `prepared: true` in config/portals.yaml.")
        return 2
    known = set(load_portals(args.config_dir))
    unknown = [e for e in economies if e not in known]
    if unknown:
        print(f"unknown Economy code(s): {', '.join(unknown)}")
        return 2

    if args.pillars:
        try:
            pillars = tuple(int(p) for p in _codes(args.pillars))
        except ValueError:
            print(f"--pillars must be numbers, got {args.pillars!r}")
            return 2
    else:
        pillars = load_default_pillars(args.config_dir)

    combos = plan_combos(tuple(economies), pillars, tuple(engine_names))
    done = completed_runs(db)
    documents = {economy: document_count(db, economy) for economy in economies}
    estimates = {
        combo.key: estimate_usd(
            combo,
            api_key_env=engines[combo.engine].api_key_env,
            open_weights=engines[combo.engine].open_weights,
            documents=documents.get(combo.economy, 0),
            config_dir=args.config_dir,
        )
        for combo in combos
    }
    todo = [combo for combo in combos if combo.key not in done]

    spent = sum(row["usd"] for row in read_ledger(ledger_path))
    planned = sum(estimates[combo.key] for combo in todo)

    print(f"database   {db}")
    print(f"data dir   {data_dir}")
    print(f"ledger     {ledger_path}  (USD {spent:.4f} already recorded)")
    print(f"ceiling    USD {args.ceiling_usd:.2f}  (stop per Run above USD {args.max_run_usd:.2f})")
    log_dir = Path(args.log_dir) if args.log_dir else ledger_path.parent / DEFAULT_LOG_DIR_NAME
    if args.parallel > 1:
        print(f"parallel   {args.parallel} Runs at once, one log per Run in {log_dir}")
    print(
        f"plan       {len(combos)} combination(s), {len(combos) - len(todo)} already"
        f" done, {len(todo)} to run"
    )
    for combo in combos:
        if combo.key in done:
            print(f"  skip {combo}: completed Run {done[combo.key]['run_id']}")
        else:
            print(f"  run  {combo}: estimated USD {estimates[combo.key]:.4f}")
    print(f"estimated total for this job: USD {planned:.4f}")

    # Before a cent is spent: does every Document these Runs will read actually
    # open under this data folder? A row pointing at a path from another
    # machine fails the Run it is read in, and in an image that is every Run.
    missing = missing_corpus_files(db, tuple(economies), data_dir)
    if missing:
        print(
            f"CORPUS FILES MISSING: {len(missing)} row(s) point at bytes that are"
            f" not under {data_dir}. Every Run over them will fail."
        )
        for row in missing:
            print(f"  {row}")
    else:
        print("corpus files: every Document of the planned Economies opens.")
    if spent + planned > args.ceiling_usd:
        print(
            f"NOTE: the whole plan would cost about USD {spent + planned:.4f},"
            f" over the ceiling of USD {args.ceiling_usd:.2f}. The job will run"
            " what fits and stop."
        )

    if not todo:
        print("nothing to run: every combination already has a completed Run Record.")
        print()
        print(render_report(read_ledger(ledger_path)))
        return 0

    if args.dry_run:
        print("--dry-run: nothing was run.")
        return 0

    if not args.yes and not _confirm(
        f"start {len(todo)} Run(s), about USD {planned:.4f}? type yes: "
    ):
        print("not confirmed; nothing was run.")
        return 2

    if args.parallel > 1:
        stopped = run_parallel(
            todo, estimates, parallel=args.parallel, launch=launch, db=db,
            data_dir=data_dir, ledger_path=ledger_path, log_dir=log_dir,
            spent=spent, args=args,
        )
        print()
        print(render_report(read_ledger(ledger_path)))
        return stopped

    stopped = 0
    for combo in todo:
        estimate = estimates[combo.key]
        if spent + estimate > args.ceiling_usd:
            print(
                f"STOP before {combo}: USD {spent:.4f} spent plus an estimated"
                f" USD {estimate:.4f} would pass the ceiling of USD"
                f" {args.ceiling_usd:.2f}. Raise --ceiling-usd to continue."
            )
            stopped = 3
            break
        print(f"\n=== {combo} (estimated USD {estimate:.4f}) ===", flush=True)
        code = execute(combo, db=db, data_dir=data_dir, concurrency=args.concurrency)
        if code != 0:
            print(f"STOP: {combo} exited {code}; nothing further was run.")
            stopped = 1
            break
        spent, stopped = _record_finished(
            combo, db=db, ledger_path=ledger_path, spent=spent, args=args,
            then=SEQUENTIAL_THEN,
        )
        if stopped:
            break

    print()
    print(render_report(read_ledger(ledger_path)))
    return stopped


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
