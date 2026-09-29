"""RegCompass CLI (Typer): every pipeline stage behind one command surface.
Commands validate their inputs and config, and say precisely what is not
available in the current install."""

from __future__ import annotations

from pathlib import Path

import typer

from regcompass import __version__
from regcompass.config import (
    load_crosswalk,
    load_default_pillars,
    load_models,
    load_pillars,
    load_pipeline,
    load_portals,
)
from regcompass.contracts import MAX_CONCURRENCY, Engine, UnknownEngine
from regcompass.engines import ConfigError, preflight_key, resolve_engine
from regcompass.storage import ClearReport, Storage

app = typer.Typer(
    name="regcompass",
    help="Map national legislation to the UN ESCAP RDTII 2.1 framework with mechanical citation enforcement.",
    no_args_is_help=True,
)


def economies() -> tuple[str, ...]:
    """Every configured Economy code. Read at call time, never at import time,
    so a test pointing the loaders at another config directory sees its own
    registry and adding an Economy stays a one-file edit."""
    return tuple(load_portals())


def _portal_language(economy: str) -> str | None:
    """The Economy's default Document Language, as Discovery records it. It is
    the honest stand-in wherever a Document has no Language of its own."""
    portal = load_portals().get(economy)
    return portal.languages[0] if portal is not None and portal.languages else None


def pillars() -> tuple[int, ...]:
    """Every configured Pillar number."""
    return tuple(sorted(load_pillars()))


def default_pillars() -> tuple[int, ...]:
    """The Pillars a Run covers when none is named. Configuration, not a
    literal: see config/pillars.json."""
    return load_default_pillars()


def _economy_help() -> str:
    """The --economy help text, built from the registry when the option is
    declared. A broken config must not turn `regcompass --help` into a
    traceback, so the fallback points at check-config instead."""
    try:
        return "Economy code, one of: " + ", ".join(economies())
    except Exception:  # noqa: BLE001 - see docstring; check-config reports the real problem
        return "Economy code from config/portals.yaml (see `regcompass check-config`)"


def _pillar_help() -> str:
    try:
        configured = pillars()
        fallback = default_pillars()
    except Exception:  # noqa: BLE001 - as _economy_help
        return "Pillar number from config/pillars.json"
    named = " and ".join(str(p) for p in fallback)
    return f"Pillar {configured[0]} to {configured[-1]} (default: {named})"


def _resolve_economy(code: str) -> str:
    """The one place a typed Economy code is checked against the registry."""
    known = economies()
    resolved = code.upper()
    if resolved not in known:
        typer.secho(
            f"Unknown economy '{code}': expected one of {', '.join(known)}", fg="red"
        )
        raise typer.Exit(code=2)
    return resolved


def _resolve_pillars(pillar: int | None) -> tuple[int, ...]:
    """The one place a typed Pillar is checked against the registry. None means
    the configured DEFAULT Pillars, never all twelve: an unqualified Run should
    do the mandatory pair, not a 61-Indicator sweep."""
    known = pillars()
    if pillar is None:
        return default_pillars()
    if pillar not in known:
        typer.secho(
            f"Unknown pillar {pillar}: expected one of"
            f" {', '.join(str(p) for p in known)}",
            fg="red",
        )
        raise typer.Exit(code=2)
    return (pillar,)


def _resolve_indicators(
    indicators: list[str] | None, run_pillars: tuple[int, ...]
) -> tuple[str, ...] | None:
    """The one place typed Indicator ids are checked against the Run's Pillars.
    None (no --indicator given) means the whole Pillar set. Exit 2, like an
    unknown Pillar: the Run was never started, so this is a bad invocation and
    not a Run that failed."""
    from regcompass.config import UnknownIndicator, narrow_indicators

    try:
        return narrow_indicators(indicators, run_pillars)
    except UnknownIndicator as e:
        typer.secho(str(e), fg="red")
        raise typer.Exit(code=2)


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader (stdlib only): KEY=VALUE lines into os.environ,
    never overriding variables already set. Keys live in .env (gitignored)
    per the repo contract; a judge running a keyed Engine just writes the file."""
    import os

    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def _quiet_litellm() -> None:
    """Gate LiteLLM's raw provider-list / feedback prints at CLI startup
    (get_llm_provider_logic.py and exception_mapping_utils.py print to stdout on
    some error paths and drown the narration). No-op on the base tier where
    litellm is not importable."""
    try:
        import litellm

        litellm.suppress_debug_info = True
    except Exception:  # noqa: BLE001 - base tier has no litellm; nothing to gate
        pass


# Stage-tag colors for the rich narration sink (keyed by the "Mx ..." prefix).
_STAGE_STYLE = {
    "M0": "bold white",
    "M10": "bold magenta",
    "M1": "cyan",
    "M2": "yellow",
    "M4": "blue",
    "M5": "blue",
    "M6": "green",
    "M7": "green",
    "M8": "magenta",
    "M9": "bold green",
    "ERROR": "bold red",
}


def _style_line(msg: str) -> str:
    tag = msg.split(" ", 1)[0]
    style = _STAGE_STYLE.get(tag, "white")
    return f"[{style}]{msg}[/{style}]"


class RichNarrator:
    """A live rich narration sink for the model-running commands: colored stage
    banners printed above a persistent M6/M7 per-pair progress bar. Falls back
    cleanly on a non-TTY (piped) stream. Used as a context manager so the
    progress bar is torn down on exit."""

    def __init__(self) -> None:
        from rich.console import Console
        from rich.progress import (
            BarColumn,
            MofNCompleteColumn,
            Progress,
            TextColumn,
            TimeElapsedColumn,
        )

        self.console = Console()
        self._progress = Progress(
            TextColumn("[bold]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=self.console,
            transient=True,
        )
        self._tasks: dict[str, int] = {}

    def __enter__(self) -> "RichNarrator":
        self._progress.start()
        return self

    def __exit__(self, *exc) -> None:
        self._progress.stop()

    def line(self, msg: str) -> None:
        self._progress.console.print(_style_line(str(msg)), highlight=False)

    def echo(self, msg: str) -> None:
        self._progress.console.print(str(msg), highlight=False)

    def pair(self, doc_id: str, i: int, total: int) -> None:
        tid = self._tasks.get(doc_id)
        if tid is None:
            tid = self._progress.add_task(f"M6/M7 {doc_id}", total=total)
            self._tasks[doc_id] = tid
        self._progress.update(tid, completed=i)


# The retired keyless lane: `--tier local` ran a 4B model on the judge's own
# machine. The Engine registry replaced the tier pair; a 4B model
# is too weak to make a fair Comparison, so the flag now points at the Engines.
_RETIRED_TIER_MESSAGE = (
    "the keyless 4B lane was retired; use --engine fake for an"
    " offline demo or --engine engine-b"
)
# One release of grace for the old flag, then it goes.
_TIER_ALIASES = {"byok": "engine-b"}


def _select_engine(engine: str | None, tier: str | None) -> Engine:
    """Resolve the Engine for a model-running command: --engine wins, --tier is
    the deprecated alias, neither means the configured default Engine. Exits 2
    with the valid names on a typo, and with the Engine and its key variable
    named when the key is missing (BEFORE any model call or database write)."""
    name = engine
    if tier is not None:
        if tier in _TIER_ALIASES:
            typer.secho(
                f"--tier is deprecated; use --engine {_TIER_ALIASES[tier]}", fg="yellow"
            )
            name = name or _TIER_ALIASES[tier]
        elif tier == "local":
            typer.secho(_RETIRED_TIER_MESSAGE, fg="red")
            raise typer.Exit(code=2)
        else:
            typer.secho(
                f"Unknown --tier '{tier}': the tier flag is deprecated; use"
                " --engine (see `regcompass engines`)",
                fg="red",
            )
            raise typer.Exit(code=2)
    try:
        selected = resolve_engine(name)
    except UnknownEngine as e:
        typer.secho(str(e), fg="red")
        raise typer.Exit(code=2)
    _load_dotenv()
    try:
        preflight_key(selected)
    except ConfigError as e:
        typer.secho(str(e), fg="red")
        raise typer.Exit(code=2)
    return selected


def _duration_s(started_at: str | None, ended_at: str | None) -> float | None:
    """Seconds between two Run Record timestamps, or None when either is
    missing (an unfinished record has no end)."""
    from datetime import datetime

    if not started_at or not ended_at:
        return None
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
    except ValueError:  # pragma: no cover - defensive
        return None
    return (end - start).total_seconds()


def _print_run_record(report, *, pillars: tuple[int, ...], status: str = "completed") -> None:
    """The Run Record as the operator reads it at the end of a Run: what ran,
    how long it took, what it spent. The same facts the runs table holds, so a
    hand-in and a terminal never disagree."""
    duration = _duration_s(report.started_at, report.ended_at)
    typer.secho("\nRun Record", fg="green", bold=True)
    typer.echo(f"  id                 {report.run_id}")
    typer.echo(f"  Economy            {report.economy}")
    typer.echo(f"  Pillars            {', '.join(str(p) for p in pillars)}")
    typer.echo(f"  Engine             {report.engine}")
    typer.echo(f"  status             {status}")
    typer.echo(f"  started            {report.started_at}")
    typer.echo(f"  ended              {report.ended_at}")
    if duration is not None:
        typer.echo(f"  duration           {duration:.1f}s")
    typer.echo(f"  Documents fetched  {report.documents_fetched}")
    concurrency = getattr(report, "engine_concurrency", None)
    if concurrency:
        line = f"  concurrency        {concurrency} Mapping call(s) in flight"
        retries = getattr(report, "rate_limit_retries", 0)
        if retries:
            line += f", {retries} rate-limit retry(ies)"
        typer.echo(line)
    typer.echo(
        f"  tokens             {report.prompt_tokens:,} prompt,"
        f" {report.completion_tokens:,} completion"
    )
    typer.echo(f"  cost               US${report.cost_usd:.6f}")
    if report.provider_cost_usd is not None:
        typer.echo(f"  provider cost      US${report.provider_cost_usd:.6f}")


def _print_output_summary(result, db: Path, out: Path) -> None:
    """Name every output file location plus the commands to inspect results, so
    a judge always knows where the artifacts landed and how to explore them."""
    from regcompass.export import duplicate_collapse_notice

    notice = duplicate_collapse_notice(result)
    if notice:
        typer.secho(f"\n{notice}", fg="yellow")
    typer.secho("\nOutputs", fg="green", bold=True)
    typer.echo(f"  submission.csv        {out / 'submission.csv'}")
    if getattr(result, "xlsx_path", None):
        typer.echo(
            f"  submission.xlsx       {result.xlsx_path}   (the organizers' workbook;"
            f" provisions only, absence rows are CSV-only)"
        )
    typer.echo(f"  submission.json       {out / 'submission.json'}")
    typer.echo(f"  supplementary.json    {out / 'supplementary.json'}")
    typer.echo(f"  per-document JSONs     {len(result.working_json_paths)} in {out}/")
    typer.echo(f"  working database      {db}")
    typer.secho("Inspect", fg="green", bold=True)
    typer.echo(f"  regcompass audit-log --db {db}      # the per-stage audit trail")
    typer.echo(f"  regcompass drops --db {db}          # rejected mappings + reasons")
    typer.echo(f"  regcompass demo                     # browse it all in the web UI")


@app.command()
def run(
    economy: str = typer.Option(..., help=_economy_help()),
    pillar: int = typer.Option(None, help=_pillar_help()),
    indicator: list[str] = typer.Option(
        None,
        "--indicator",
        help="Narrow the Run to this Indicator id; repeat for several"
        " (default: every Indicator of the Pillar)",
    ),
    engine: str = typer.Option(
        None, help="Engine name from config/models.yaml (see `regcompass engines`)"
    ),
    tier: str = typer.Option(
        None, help="DEPRECATED alias for --engine; 'byok' means --engine engine-b"
    ),
    concurrency: int = typer.Option(
        None,
        "--concurrency",
        min=1,
        max=MAX_CONCURRENCY,
        help="Mapping calls to keep in flight (default: the Engine's own value,"
        " 4 on a hosted Engine and 1 on the fake one). Records and exports are"
        " identical whatever it is.",
    ),
    db: Path = typer.Option(Path("data/regcompass.db"), help="SQLite working database"),
    data_dir: Path = typer.Option(
        Path("data"), help="Root the Corpus's stored bytes live under"
    ),
) -> None:
    """Run the storage-attached pipeline (M1 extract .. M8 reconcile) over one
    Economy's CORPUS on the selected Engine. A Run never fetches: fill the
    Corpus first with `regcompass discover --economy <code>`, then run any
    Engine over it as often as you like. Every stage logs to audit_log; the
    verified records land in the mappings table; `regcompass export` then ships
    them. A keyed Engine needs its variable in .env; every Engine except the
    fake one needs Ollama running with bge-m3 pulled (the gate embeds locally
    whichever Engine answers)."""
    from regcompass.pipeline import EmptyCorpusError, run_economy

    economy = _resolve_economy(economy)
    run_pillars = _resolve_pillars(pillar)
    run_indicators = _resolve_indicators(indicator, run_pillars)
    # Fail fast on a missing key: without this, every (chunk, indicator) pair
    # would burn the transport-retry backoff on an error no retry can fix and
    # the run would finish green with zero records.
    selected = _select_engine(engine, tier)
    _quiet_litellm()
    db.parent.mkdir(parents=True, exist_ok=True)
    storage = Storage(db)
    storage.apply_schema()
    scope = (
        f"pillars={run_pillars}" if run_indicators is None
        else f"pillars={run_pillars} indicators={','.join(run_indicators)}"
    )
    typer.secho(
        f"regcompass {__version__} | run economy={economy} {scope}"
        f" engine={selected.name}",
        fg="white", bold=True,
    )
    try:
        with RichNarrator() as narr:
            report = run_economy(
                storage, economy, run_pillars, selected, data_dir=data_dir,
                indicators=None if run_indicators is None else list(run_indicators),
                progress=narr.line, pair_progress=narr.pair,
                concurrency=concurrency,
            )
    except EmptyCorpusError as e:
        # Not a failure, a missing prerequisite: the message names the command
        # that fixes it, and exit 2 keeps it apart from a Run that broke.
        typer.secho(str(e), fg="yellow")
        # Discovery needs the internet, and the keyless demo is for a judge who
        # may have neither a network nor a key. Where
        # bundled legislation exists, the offline step goes on the same line;
        # where it does not, pointing at a command that would refuse them is
        # worse than pointing at nothing.
        from regcompass.fixtures import seedable_economies

        if economy in seedable_economies():
            typer.secho(
                f"offline alternative: `regcompass seed --economy {economy}`"
                " loads the bundled fixture legislation (demo only)",
                fg="yellow",
            )
        raise typer.Exit(code=2)
    except (RuntimeError, OSError) as e:
        # Same clean-error pattern as `export`: the actionable message (dead
        # Ollama, a Corpus Document missing on disk, a persisted transport
        # failure) without the multi-frame traceback.
        typer.secho(str(e), fg="red")
        raise typer.Exit(code=1)
    finally:
        # Folds the write-ahead log back into the database file, so a Run that
        # ends leaves one complete file behind (see Storage.close).
        storage.close()
    typer.echo(
        f"{economy}: {len(report.documents)} documents, {report.n_pairs_gated} gated pairs ->"
        f" {report.n_passed} verified, {report.n_no_evidence} no-evidence,"
        f" {report.n_dropped} dropped, {report.n_groups} groups reconciled"
    )
    _print_run_record(report, pillars=run_pillars)
    typer.secho(f"\nrecords in {db}; next: regcompass export --db {db}", fg="green")
    typer.echo(f"this Run Record: regcompass runs show {report.run_id} --db {db}")


runs_app = typer.Typer(
    help="Past Run Records: what ran, what it fetched, what it cost.",
    no_args_is_help=False,
)
app.add_typer(runs_app, name="runs")


def _open_runs(db: Path):
    """The Storage behind the runs table, or None with a printed reason. A
    listing must never CREATE a database: pointing at the wrong path should say
    so, not leave an empty file behind."""
    if not db.exists():
        typer.secho(f"no database at {db}: run something first", fg="yellow")
        return None
    storage = Storage(db)
    if "runs" not in storage.table_names():
        typer.secho(
            f"{db} has no runs table (it predates Run Records); start a new run"
            " to create one",
            fg="yellow",
        )
        storage.close()
        return None
    return storage


@runs_app.callback(invoke_without_command=True)
def runs(
    ctx: typer.Context,
    economy: str = typer.Option(None, help="Only this Economy's Run Records"),
    limit: int = typer.Option(20, help="How many records to list"),
    db: Path = typer.Option(Path("data/regcompass.db"), help="SQLite working database"),
) -> None:
    """List past Run Records, newest first (Runs and Discoveries)."""
    if ctx.invoked_subcommand is not None:
        return
    storage = _open_runs(db)
    if storage is None:
        return
    try:
        records = storage.runs_list(
            economy=None if economy is None else economy.upper(), limit=limit
        )
    finally:
        storage.close()
    if not records:
        typer.secho("no Run Records yet", fg="yellow")
        return
    typer.secho(f"{len(records)} Run Record(s), newest first", fg="green", bold=True)
    for rec in records:
        duration = _duration_s(rec["started_at"], rec["ended_at"])
        typer.echo(
            f"  {rec['run_id']}  {rec['kind']:9} {rec['economy']:3}"
            f"  pillars={','.join(str(p) for p in rec['pillars']) or '-':7}"
            f"  engine={rec['engine'] or '-':10}"
            f"  {rec['status']:9}"
            f"  {'-' if duration is None else format(duration, '.1f') + 's':>8}"
            f"  fetched={rec['documents_fetched']:<4}"
            f"  tokens={rec['prompt_tokens']:,}/{rec['completion_tokens']:,}"
            f"  US${rec['cost_usd']:.6f}"
        )
    typer.echo(f"\none record in full: regcompass runs show <run_id> --db {db}")


@runs_app.command("show")
def runs_show(
    run_id: str = typer.Argument(..., help="The Run Record id"),
    db: Path = typer.Option(Path("data/regcompass.db"), help="SQLite working database"),
) -> None:
    """Print one Run Record as JSON (the downloadable hand-in shape)."""
    import json

    from regcompass.contracts import RunRecord

    storage = _open_runs(db)
    if storage is None:
        raise typer.Exit(code=1)
    try:
        row = storage.run_get(run_id)
    finally:
        storage.close()
    if row is None:
        typer.secho(f"no Run Record '{run_id}' in {db}", fg="red")
        raise typer.Exit(code=1)
    typer.echo(json.dumps(RunRecord.from_row(row).model_dump(), indent=2))


@app.command()
def discover(
    economy: str = typer.Option(..., help=_economy_help()),
    refresh: bool = typer.Option(
        False, "--refresh", help="Re-fetch Documents already in the Corpus and replace them"
    ),
    db: Path = typer.Option(Path("data/regcompass.db"), help="SQLite working database"),
    data_dir: Path = typer.Option(Path("data"), help="Root for <economy>/raw/ exact bytes"),
    max_documents: int = typer.Option(None, help="Stop after N Documents (smoke runs)"),
) -> None:
    """Fill one Economy's Corpus from its official Portal. Discovery is the ONLY
    step that touches the internet: it honours robots.txt, keeps one connection
    at the Portal's spacing floor under an identified user agent, and stores each
    Document with its exact bytes, its Source URL, its Language and its fetch
    time. Documents already in the Corpus are skipped without a request; pass
    --refresh to re-fetch them. Then `regcompass run` reads the Corpus and never
    fetches."""
    _discover(economy, refresh=refresh, db=db, data_dir=data_dir, max_documents=max_documents)


@app.command()
def seed(
    economy: str = typer.Option(
        None, help="Economy code to seed; omit and pass --all-fixtures for every bundled one"
    ),
    all_fixtures: bool = typer.Option(
        False, "--all-fixtures", help="Seed every Economy that carries bundled legislation"
    ),
    db: Path = typer.Option(Path("data/regcompass.db"), help="SQLite working database"),
    data_dir: Path = typer.Option(Path("data"), help="Root for <economy>/raw/ exact bytes"),
    language: str = typer.Option(
        None, help="Language for the seeded Documents (default: the Economy's Portal Language)"
    ),
) -> None:
    """Fill a Corpus OFFLINE from the legislation bundled with this install.

    Discovery is the honest way to fill a Corpus and it needs the internet. This
    is the demo way: the legislation PDFs that ship with the source go into the
    Corpus through the same write path, so `regcompass run --engine fake` has
    something to read on a clean machine with no key and no network. Every row
    is marked source kind 'fixture', which the Document list shows and the
    Evidence Export discloses in Notes: a seeded Corpus is a demonstration,
    never a collection. Safe to run twice."""
    from regcompass.corpus import FIXTURE_DISCLOSURE
    from regcompass.fixtures import (
        FixtureLegislationMissingError,
        seed_economy,
        seedable_economies,
    )

    if bool(economy) == bool(all_fixtures):
        typer.secho(
            "name exactly one of --economy <code> or --all-fixtures"
            f" (bundled Economies: {', '.join(seedable_economies())})",
            fg="red",
        )
        raise typer.Exit(code=2)

    targets = list(seedable_economies()) if all_fixtures else [_resolve_economy(economy)]
    db.parent.mkdir(parents=True, exist_ok=True)
    storage = Storage(db)
    storage.apply_schema()
    typer.secho(
        f"regcompass {__version__} | seed {', '.join(targets)} ({FIXTURE_DISCLOSURE})",
        fg="white", bold=True,
    )
    for code in targets:
        try:
            result = seed_economy(
                storage, data_dir, code, language=language or None
            )
        except FixtureLegislationMissingError as e:
            typer.secho(str(e), fg="yellow")
            raise typer.Exit(code=2)
        for added in result.added:
            typer.secho(
                f"  {code}: added {added.document_id} ({added.n_pages} pages,"
                f" {result.language})",
                fg="green",
            )
        for document_id in result.already_present:
            typer.echo(f"  {code}: already seeded {document_id}")
    typer.secho(
        f"\nCorpus in {db}; next: regcompass run --engine fake --economy {targets[0]}"
        " --pillar 7",
        fg="green",
    )


@app.command("load-data")
def load_data(
    url: str = typer.Option(
        None, help="Where the prepared-data archive is published (default: config/prepared_data.yaml)"
    ),
    sha256: str = typer.Option(
        None, help="The archive's SHA-256 from the release notes (default: config/prepared_data.yaml)"
    ),
    db: Path = typer.Option(
        Path("data/regcompass.db"), envvar="REGCOMPASS_DB",
        help="Working database to put in place",
    ),
    data_dir: Path = typer.Option(
        Path("data"), envvar="REGCOMPASS_DATA",
        help="Root the Corpus bytes are unpacked under (<economy>/raw/)",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Replace a database that already holds Documents or Runs, and stored files with other bytes",
    ),
) -> None:
    """Fill an empty data folder with the prepared database a release ships:
    the pre-run Runs, their Corpus and the source files the audit view shows.

    The archive is downloaded, its SHA-256 is checked against the one the
    release names BEFORE anything is unpacked, and a mismatch stops with
    nothing changed. A database that already holds Documents or Runs is never
    replaced without --force. Stop the server first if it is running on the
    same database: Ctrl+C on `regcompass serve`, or `docker compose stop
    regcompass` with Docker."""
    from regcompass.prepared_data import (
        PreparedDataError,
        configured_source,
        load_prepared_data,
    )

    configured = configured_source()
    url = url or (configured.url if configured else None)
    sha256 = sha256 or (configured.sha256 if configured else None)
    if not url or not sha256:
        typer.secho(
            "name the archive with --url and its --sha256 (both are in the release"
            " notes); this install's config/prepared_data.yaml does not carry them",
            fg="red",
        )
        raise typer.Exit(code=2)
    typer.secho(
        f"regcompass {__version__} | load-data (db={db}, data={data_dir})",
        fg="white", bold=True,
    )
    try:
        report = load_prepared_data(
            url, sha256, db_path=db, data_dir=data_dir, force=force,
            progress=typer.echo,
        )
    except PreparedDataError as e:
        typer.secho(str(e), fg="red")
        raise typer.Exit(code=1)
    for code, entry in sorted(report.manifest.get("economies", {}).items()):
        typer.echo(
            f"  {code}: {_plural(entry.get('documents', 0), 'Document')},"
            f" {_plural(len(entry.get('runs', [])), 'Run')}"
        )
    typer.secho(
        f"\nloaded {_plural(report.documents, 'Document')} and"
        f" {_plural(report.runs, 'Run')} into {db}"
        f" ({report.raw_files_written} source files written,"
        f" {report.raw_files_already_present} already in place);"
        " start the server and open the Runs list",
        fg="green",
    )


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _readable_bytes(n: int) -> str:
    """A size a person reads at a glance, not a byte count they have to parse."""
    if n < 1024:
        return f"{n} bytes"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _clear_lines(report: ClearReport) -> list[str]:
    """What a clear takes, in plain words, for the confirmation and the receipt."""
    lines = [
        f"  {_plural(report.documents, 'Document')} in the Corpus",
        f"  {_plural(report.stored_files, 'stored file')}"
        f" ({_readable_bytes(report.bytes)})",
        f"  {_plural(report.extractions, 'stored text stream')}",
        f"  {_plural(report.runs, 'Run Record')},"
        f" {_plural(report.mappings, 'Mapping')},"
        f" {_plural(report.reviews, 'Review Decision')}",
    ]
    if report.refused_files:
        lines.append(
            f"  {_plural(report.refused_files, 'stored file')} outside the data"
            " root: left alone"
        )
    return lines


@app.command()
def clear(
    economy: str = typer.Option(None, help=_economy_help()),
    all_economies: bool = typer.Option(
        False, "--all", help="Clear every Economy, not one"
    ),
    db: Path = typer.Option(Path("data/regcompass.db"), help="SQLite working database"),
    data_dir: Path = typer.Option(Path("data"), help="Root for <economy>/raw/ exact bytes"),
    yes: bool = typer.Option(
        False, "--yes", help="Skip the typed confirmation (for a runbook)"
    ),
) -> None:
    """Clear the downloaded Documents and every cache, for one Economy or for all.

    The step before a sealed test, when a steward asks to see the tool start
    empty: the Corpus rows and the bytes they were stored as, the stored text
    streams a second pass would have reused, and the Run Records with their
    Mappings and Review Decisions. Nothing outside --data-dir is touched. The
    counts are printed first and a typed `yes` is what starts it.

    Do NOT run this against a database a running server holds open: this lane
    cannot see whether a Run or a Discovery is in flight, and it would delete
    rows from under it. A live server has the same control on its Settings
    screen, and that one refuses while a job is active.

    After this the next Run fetches and extracts again."""
    if bool(economy) == bool(all_economies):
        typer.secho("name exactly one of --economy <code> or --all", fg="red")
        raise typer.Exit(code=2)
    scope = None if all_economies else _resolve_economy(economy)
    where = "every Economy" if scope is None else scope
    typer.secho(
        f"regcompass {__version__} | clear {where} (db={db}, data={data_dir})",
        fg="white", bold=True,
    )
    if not db.is_file():
        typer.secho(f"no working database at {db}: nothing to clear", fg="yellow")
        return
    storage = Storage(db)
    storage.apply_schema()
    preview = storage.clear_preview(scope, data_dir=data_dir)
    typer.echo("this will remove:")
    for line in _clear_lines(preview):
        typer.secho(line, fg="yellow")
    if not yes:
        answer = typer.prompt("type yes to clear, anything else to stop", default="no")
        if answer.strip().lower() != "yes":
            typer.secho("nothing was cleared", fg="green")
            raise typer.Exit(code=1)
    removed = storage.clear(scope, data_dir=data_dir)
    typer.secho("removed:", fg="green", bold=True)
    for line in _clear_lines(removed):
        typer.secho(line, fg="green")
    typer.secho(
        f"\nthe Corpus and the Runs are empty for {where};"
        " the next Run fetches and extracts again",
        fg="green",
    )


@app.command("repair-pages")
def repair_pages(
    db: Path = typer.Option(Path("data/regcompass.db"), help="SQLite working database"),
    run_id: str = typer.Option(
        None, "--run", help="Repair one Run only (default: every Run)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Count and list the moves, write nothing"
    ),
) -> None:
    """Correct the cited page of stored Mappings to the page the quote is on.

    Mappings stored before the Map step placed each Verbatim Quote on its page
    carry the page their Piece starts on, which can be pages before the quote
    (the Evidence Export's "PDF: page N" and the Open source link then land
    early). This re-derives every page from the quote's position in the
    Document's stored text. No Engine call; only page_number changes, and a
    second pass moves nothing. Do not run it while a Run writes to the same
    database."""
    from regcompass.pipeline import repair_page_numbers

    scope = f"Run {run_id}" if run_id else "every Run"
    typer.secho(
        f"regcompass {__version__} | repair-pages {scope} (db={db})"
        + (" | dry run" if dry_run else ""),
        fg="white", bold=True,
    )
    if not db.is_file():
        typer.secho(f"no working database at {db}", fg="red")
        raise typer.Exit(code=2)
    # A dry run opens the file read-only: no schema pass, no journal switch,
    # nothing on disk changes.
    if dry_run:
        storage = Storage.read_only(db)
    else:
        storage = Storage(db)
        storage.apply_schema()
    try:
        report = repair_page_numbers(storage, run_id=run_id, dry_run=dry_run)
    except LookupError as e:
        typer.secho(str(e), fg="red")
        raise typer.Exit(code=2)
    finally:
        storage.close()
    for m in report.moves:
        typer.echo(f"  {m.run_id}  {m.mapping_id}: page {m.old} -> {m.new}")
    verb = "would move" if dry_run else "moved"
    typer.secho(
        f"{report.moved} of {report.examined} Mappings {verb} page"
        f" ({report.undetermined} could not be placed and were left as they were;"
        f" {report.no_quote} no-evidence rows carry no quote)",
        fg="green",
    )


def _discover(
    economy: str,
    *,
    refresh: bool,
    db: Path,
    data_dir: Path,
    max_documents: int | None,
) -> None:
    """The Discovery lane behind both `discover` and its deprecated alias
    `crawl`. One body, so neither name can drift into a second way of reaching
    the internet."""
    from regcompass.discovery import ManualEconomyError, discover_economy

    economy = _resolve_economy(economy)
    db.parent.mkdir(parents=True, exist_ok=True)
    storage = Storage(db)
    storage.apply_schema()
    typer.secho(
        f"regcompass {__version__} | discover economy={economy} refresh={refresh}",
        fg="white", bold=True,
    )
    try:
        report = discover_economy(
            economy, storage, refresh=refresh, data_dir=data_dir,
            max_documents=max_documents,
            progress=lambda m: typer.secho(m, fg="magenta"),
        )
    except ManualEconomyError as e:
        typer.secho(str(e), fg="yellow")
        raise typer.Exit(code=2)
    except (RuntimeError, OSError) as e:
        typer.secho(str(e), fg="red")
        raise typer.Exit(code=1)

    language = load_portals()[economy].languages[0]
    typer.echo(
        f"{economy}: strategy={report.strategy} fetched={report.fetched}"
        f" skipped_existing={report.skipped_existing}"
        f" stored={report.documents_stored} deduplicated={report.deduplicated}"
        f" disallowed={report.disallowed} failed={report.failed}"
        f" ({language}, floor {report.min_interval_seconds}s/host)"
    )
    if report.fetched_without_a_row:
        typer.secho(
            f"  {report.fetched_without_a_row} fetched file(s) have no Corpus row:"
            f" {economy} holds fewer Documents than the Portal gave us."
            " A file this Discovery could not ingest is named in the misses;"
            " one left over from an earlier Discovery is added by running"
            " discover again.",
            fg="yellow",
        )
    if report.escalated_to_impersonation:
        typer.secho(
            "  the Portal refused our identified user agent; escalated to the"
            " documented browser-impersonation rung",
            fg="yellow",
        )
    for miss in report.misses:
        typer.secho(f"  miss: {miss}", fg="yellow")
    typer.secho(
        f"Discovery record {report.run_id} in {db};"
        f" next: regcompass run --economy {economy} --db {db} --data-dir {data_dir}",
        fg="green",
    )


@app.command()
def crawl(
    economy: str = typer.Option(..., help=_economy_help()),
    db: Path = typer.Option(Path("data/regcompass.db"), help="SQLite working database"),
    data_dir: Path = typer.Option(Path("data"), help="Root for <economy>/raw/ exact bytes"),
    refresh: bool = typer.Option(
        False, "--refresh", help="Re-fetch Documents already in the Corpus and replace them"
    ),
    max_documents: int = typer.Option(None, help="Stop after N Documents (smoke runs)"),
) -> None:
    """DEPRECATED alias for `regcompass discover`. It used to reach the fetch
    machinery directly, which meant no single connection, no robots.txt
    Disallow check and no Discovery record; it is now the same lane under the
    old name, because Discovery is the only step that touches the internet."""
    typer.secho(
        "`regcompass crawl` is deprecated; use `regcompass discover` (same lane,"
        " and it fills the Corpus and files a Discovery record).",
        fg="yellow",
    )
    _discover(economy, refresh=refresh, db=db, data_dir=data_dir, max_documents=max_documents)


@app.command()
def shortlist(
    economy: str = typer.Option("all", help=_economy_help() + "; or 'all'"),
    pillar: int = typer.Option(None, help=_pillar_help()),
    db: Path = typer.Option(Path("data/regcompass.db"), help="SQLite working database"),
    data_dir: Path = typer.Option(Path("data"), help="Root for <economy>/raw/ exact bytes"),
    out_dir: Path = typer.Option(Path("data/shortlist"), help="Where the ranked CSVs go"),
) -> None:
    """Shortlist (M11): extract + embed the crawled corpus, then write one
    ranked CSV per (economy, pillar). Ranking is review order, never
    exclusion; anything that cannot be ingested is reported with a reason."""
    from regcompass.shortlist import embed_economy, ingest_economy, rank_economy

    # "all" means every PREPARED Economy: the ones whose Corpus has actually
    # been fetched. Ranking an Economy with no crawled bytes would report an
    # empty shortlist as though it were a finding.
    if economy.lower() == "all":
        selected_economies = tuple(
            code for code, p in load_portals().items() if p.prepared
        )
    else:
        selected_economies = (_resolve_economy(economy),)
    run_pillars = _resolve_pillars(pillar)
    storage = Storage(db)
    storage.apply_schema()
    for eco in selected_economies:
        # The Portal's default Language, exactly as Discovery passes it: it is
        # what picks the tesseract language data for a scanned Document.
        results, excluded = ingest_economy(
            storage, data_dir, eco, language=_portal_language(eco),
        )
        typer.echo(f"{eco}: ingested {len(results)} new documents")
        for e in excluded:
            typer.secho(f"  excluded: {e['local_path']}: {e['reason']}", fg="yellow")
        n = embed_economy(storage, eco)
        typer.echo(f"{eco}: embedded {n} documents")
        for p in run_pillars:
            rows, report = rank_economy(storage, eco, p, out_dir=out_dir)
            typer.echo(
                f"{eco} pillar {p}: ranked {len(rows)} documents in"
                f" {report.duration_s:.1f}s -> {report.csv_path}"
            )
            for note in report.notes:
                typer.secho(f"  note: {note}", fg="yellow")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address"),
    port: int = typer.Option(8000, help="Port"),
    db: Path = typer.Option(
        Path("data/regcompass.db"),
        envvar="REGCOMPASS_DB",
        help="Working database a Run writes and the audit view reads",
    ),
    out: Path = typer.Option(
        Path("out"), envvar="REGCOMPASS_OUT", help="Export directory",
    ),
    data_dir: Path = typer.Option(
        Path("data"),
        envvar="REGCOMPASS_DATA",
        help="Root for <economy>/raw/ Corpus bytes; must match the --data-dir Discovery fetched them with",
    ),
    bundle: Path = typer.Option(
        None,
        help="Read the audit view from this frozen bundle directory (manifest.json)"
        " instead of the working database: the keyless review path",
    ),
) -> None:
    """Serve the RegCompass interface: one URL with the Run panel (Economy,
    Pillar, Indicators, Engine, Run), the live progress log, the Runs list, the
    audit view (source PDF with the Verbatim Quote highlighted, accept / reject
    / flag), the Evidence Export and Settings.

    The audit view reads the working database by default, so a Run started here
    is reviewable here and its Review Decisions are stored in that same
    database, beside the Mappings they judge. --bundle points the audit view at
    a frozen bundle instead, which needs neither a key nor a Run and is read
    only: Review Decisions need a Run. Every Engine except the fake one needs a key
    (shell environment, .env, or typed into Settings) and Ollama for the Gate
    embeddings."""
    import uvicorn

    from regcompass.server import create_app

    manifest = None
    if bundle is not None:
        manifest = bundle / "manifest.json"
        if not manifest.exists():
            typer.secho(f"No manifest.json in {bundle}", fg="red")
            raise typer.Exit(code=2)
    # A keyed Engine reads its variable at run time: load .env at server start
    # like every other key-using command.
    _load_dotenv()
    try:
        app_ = create_app(
            db_path=db, out_dir=out, data_dir=data_dir, bundle_manifest=manifest,
        )
    except ValueError as exc:
        typer.secho(f"RegCompass not started: {exc}", fg="red")
        raise typer.Exit(code=2)
    where = f"bundle={bundle}" if manifest is not None else f"db={db}, data={data_dir}"
    typer.secho(f"RegCompass: http://{host}:{port}  ({where}, out={out})", fg="green")
    # Whether a login guards it, never the login itself.
    typer.echo(
        "login on (REGCOMPASS_AUTH_USER and REGCOMPASS_AUTH_PASSWORD are set)"
        if app_.state.login
        else "login off"
    )
    uvicorn.run(app_, host=host, port=port)


@app.command()
def audit(
    bundle: Path = typer.Option(
        ..., help="Audit bundle directory containing manifest.json (pipeline outputs + PDFs)"
    ),
    db: Path = typer.Option(
        Path("data/regcompass.db"), help="Working database (the audit view reads the bundle)"
    ),
    host: str = typer.Option("127.0.0.1", help="Bind address"),
    port: int = typer.Option(8000, help="Port"),
) -> None:
    """DEPRECATED alias for `regcompass serve --bundle <dir>`."""
    typer.secho("note: `audit` is now `serve --bundle <dir>`; running that.", fg="yellow")
    serve(
        host=host, port=port, db=db, out=Path("out"),
        data_dir=Path("data"), bundle=bundle,
    )


@app.command("audit-log")
def audit_log(
    db: Path = typer.Option(Path("data/regcompass.db"), help="SQLite working database"),
) -> None:
    """Inspect the audit log of a working database."""
    if not db.exists():
        typer.secho(f"No database at {db}", fg="red")
        raise typer.Exit(code=2)
    storage = Storage(db)
    rows = storage.conn.execute(
        "SELECT stage, method, decision, duration_ms, timestamp FROM audit_log ORDER BY id DESC LIMIT 20"
    ).fetchall()
    if not rows:
        typer.echo("audit_log is empty.")
        raise typer.Exit()
    for r in rows:
        typer.echo(
            f"{r['timestamp']}  {r['stage']:<14} {r['method']:<28} {r['decision']:<24} {r['duration_ms']:.1f}ms"
        )


@app.command()
def export(
    db: Path = typer.Option(Path("data/regcompass.db"), help="SQLite working database"),
    out: Path = typer.Option(Path("out"), help="Output directory"),
    run_id: str = typer.Option(
        None,
        "--run-id",
        help="Export this Run's records (default: the newest completed Run;"
        " see `regcompass runs`)",
    ),
    check_liveness: bool = typer.Option(
        False, help="Verify every source URL live (network); default off - the judge path works offline"
    ),
) -> None:
    """Export the 13-column submission file + supplementary JSON (M9) for ONE
    Run from the working database. No model calls: records, chunk texts, and
    gate cosines all come off storage; the full export battery (incl. the
    controlling-pointer gate) runs before anything is written. Without --run-id
    the newest completed Run is exported, and the run is named in the audit
    trail either way."""
    from regcompass.export import live_url_ok
    from regcompass.pipeline import NoCompletedRunError, export_from_db

    crosswalk = load_crosswalk()
    if not db.exists():
        typer.secho(f"No database at {db}: run `regcompass run` first", fg="red")
        raise typer.Exit(code=2)
    storage = Storage(db)
    typer.echo(f"Indicator emission scheme: {crosswalk.emission_scheme}")
    try:
        result = export_from_db(
            storage, out, run_id=run_id,
            liveness_fn=live_url_ok if check_liveness else None,
            progress=lambda m: typer.secho(m, fg="cyan"),
        )
    except NoCompletedRunError as e:
        # A missing prerequisite, like an empty Corpus: exit 2 keeps it apart
        # from an export that actually broke.
        typer.secho(str(e), fg="yellow")
        raise typer.Exit(code=2)
    except RuntimeError as e:
        typer.secho(str(e), fg="red")
        raise typer.Exit(code=1)
    typer.secho(
        f"battery GREEN: {len(result.rows)} rows -> {result.csv_path}"
        f" (+ supplementary JSON, {len(result.working_json_paths)} working JSONs)",
        fg="green",
    )
    if result.rows_cut:
        # The organizers' entry area holds 101 rows and every formula that
        # counts it stops at row 109, so the cut is structural, not a choice.
        typer.secho(
            f"row cap: {result.rows_cut} battery-green row(s) left out; the"
            f" Economies took turns by Confidence so every one of them appears",
            fg="yellow",
        )
    _print_output_summary(result, db, out)


@app.command()
def e2e(
    economy: str = typer.Option(..., help=_economy_help()),
    pillar: int = typer.Option(None, help=_pillar_help()),
    indicator: list[str] = typer.Option(
        None,
        "--indicator",
        help="Narrow the Run to this Indicator id; repeat for several"
        " (default: every Indicator of the Pillar)",
    ),
    engine: str = typer.Option(
        None, help="Engine name from config/models.yaml (see `regcompass engines`)"
    ),
    tier: str = typer.Option(
        None, help="DEPRECATED alias for --engine; 'byok' means --engine engine-b"
    ),
    max_documents: int = typer.Option(2, help="Bound the live crawl to N documents"),
    concurrency: int = typer.Option(
        None,
        "--concurrency",
        min=1,
        max=MAX_CONCURRENCY,
        help="Mapping calls to keep in flight during the Run (default: the"
        " Engine's own value)",
    ),
    db: Path = typer.Option(Path("data/regcompass.db"), help="SQLite working database"),
    data_dir: Path = typer.Option(Path("data"), help="Root for <economy>/raw/ crawled bytes"),
    out: Path = typer.Option(Path("out"), help="Export output directory"),
) -> None:
    """Discovery then a Run, in that order, with the export at the end: the
    convenience wrapper over the two separate lanes, so Task 1 and Task 2 stay
    one scriptable command. Discovery fetches this Economy's Corpus from its
    Portal (politeness floor intact); the Run then reads that Corpus and makes
    no request, flowing through extract -> chunk -> gate -> map -> mechanical
    verify -> reconcile -> the 13-column export. Discovered Documents are
    outside the curated corpus, so the export synthesizes a CorpusDoc per
    Document (Source URL from the Discovery record keeps the Portal whitelist
    satisfied; the title-derived law name is flagged in Notes). Point
    --db / --data-dir / --out at scratch locations to keep data/ clean."""
    from regcompass.pipeline import run_e2e

    economy = _resolve_economy(economy)
    run_pillars = _resolve_pillars(pillar)
    run_indicators = _resolve_indicators(indicator, run_pillars)
    selected = _select_engine(engine, tier)
    _quiet_litellm()
    db.parent.mkdir(parents=True, exist_ok=True)
    storage = Storage(db)
    storage.apply_schema()
    scope = (
        f"pillars={run_pillars}" if run_indicators is None
        else f"pillars={run_pillars} indicators={','.join(run_indicators)}"
    )
    typer.secho(
        f"regcompass {__version__} | e2e economy={economy} {scope}"
        f" engine={selected.name} max_documents={max_documents}",
        fg="white", bold=True,
    )
    try:
        with RichNarrator() as narr:
            report = run_e2e(
                storage, economy, run_pillars, selected, data_dir, out,
                max_documents=max_documents,
                indicators=None if run_indicators is None else list(run_indicators),
                progress=narr.line, pair_progress=narr.pair,
                concurrency=concurrency,
            )
    except (RuntimeError, OSError) as e:
        typer.secho(str(e), fg="red")
        raise typer.Exit(code=1)
    run = report.run
    typer.secho(
        f"\n{economy}: crawl fetched={report.crawl_fetched} deduplicated={report.crawl_deduplicated}"
        f" failed={report.crawl_failed} -> mapped {len(report.documents_mapped)} document(s):"
        f" {run.n_pairs_gated} gated pairs -> {run.n_passed} verified,"
        f" {run.n_no_evidence} no-evidence, {run.n_dropped} dropped, {run.n_groups} groups",
        fg="green",
    )
    _print_output_summary(report.export, db, out)


@app.command("map-pdf")
def map_pdf(
    pdf: Path = typer.Option(..., help="Path to the PDF (born-digital or scanned)"),
    economy: str = typer.Option(..., help=_economy_help()),
    pillar: int = typer.Option(None, help=_pillar_help()),
    engine: str = typer.Option(
        None, help="Engine name from config/models.yaml (see `regcompass engines`)"
    ),
    tier: str = typer.Option(
        None, help="DEPRECATED alias for --engine; 'byok' means --engine engine-b"
    ),
    law_name: str = typer.Option(..., help="Law name for the submission row"),
    source_url: str = typer.Option(..., help="Source URL for the submission row"),
    law_number: str = typer.Option(None, help="Optional Law Number / Ref"),
    last_amended: str = typer.Option(None, help="Optional Last Amended value"),
    allow_any_host: bool = typer.Option(
        False, "--allow-any-host",
        help="Permit a Source URL host outside the Round 1 portal whitelist",
    ),
    db: Path = typer.Option(Path("data/regcompass.db"), help="SQLite working database"),
    out: Path = typer.Option(Path("out"), help="Export output directory"),
) -> None:
    """Map ONE user-supplied PDF end to end: run_document auto-registers the
    documents row and auto-OCRs a scanned input (should_ocr fires when the
    born-digital text lane is unusable), then reconcile + the 13-column export.
    doc_id = doc_<eco>_user_<sha12>. This lane is a demo aid, NOT part of the
    Round 1 submission artifacts: pass --allow-any-host for a source outside the
    official portal whitelist (it stamps that disclosure in Notes)."""
    import hashlib

    from regcompass.contracts import CorpusDoc
    from regcompass.export import SyntheticDoc
    from regcompass.pipeline import (
        RunReport,
        export_from_db,
        reconcile_economy,
        record_run,
        run_document,
    )

    economy = _resolve_economy(economy)
    run_pillars = _resolve_pillars(pillar)
    selected = _select_engine(engine, tier)
    if not pdf.exists():
        typer.secho(f"No PDF at {pdf}", fg="red")
        raise typer.Exit(code=2)
    _quiet_litellm()
    sha12 = hashlib.sha256(pdf.read_bytes()).hexdigest()[:12]
    doc_id = f"doc_{economy.lower()}_user_{sha12}"
    db.parent.mkdir(parents=True, exist_ok=True)
    storage = Storage(db)
    storage.apply_schema()
    typer.secho(
        f"regcompass {__version__} | map-pdf {pdf.name} economy={economy}"
        f" pillars={run_pillars} engine={selected.name} doc_id={doc_id}",
        fg="white", bold=True,
    )
    report = RunReport(economy=economy, engine=selected.name)
    try:
        # One PDF is still a Run: it gets a Run Record like any other, so its
        # tokens and cost are on the same ledger.
        with RichNarrator() as narr, record_run(
            storage, report, economy=economy, pillars=run_pillars, engine=selected,
        ) as run_id:
            run_document(
                storage, doc_id, pdf, economy, run_pillars, selected, run_id,
                report=report, progress=narr.line, pair_progress=narr.pair,
                source_url=source_url, title=law_name, filename_hint=pdf.name,
                # No Corpus row to read a Language off, so the Economy's Portal
                # default stands in: it picks the OCR language data, and the
                # Gate's script test corrects it if the file says otherwise.
                language=_portal_language(economy),
            )
            reconcile_economy(
                storage, economy, selected, report, run_id=run_id, progress=narr.line,
            )
            result = None
            if report.n_passed:
                override = {
                    doc_id: SyntheticDoc(
                        CorpusDoc(
                            economy=economy, law_name=law_name,
                            law_number_ref=law_number, last_amended=last_amended,
                            source_url=source_url, url_is_direct=True,
                        ),
                        law_name_mechanical=False,
                        allow_any_host=allow_any_host,
                    )
                }
                result = export_from_db(
                    storage, out, synthetic_docs=override, run_id=run_id,
                    progress=narr.line,
                )
    except (RuntimeError, OSError) as e:
        typer.secho(str(e), fg="red")
        # The Run Record was filed as failed with this error before the
        # exception got here, so name it: a run that died is still a run the
        # operator can look up.
        _print_run_record(report, pillars=run_pillars, status="failed")
        raise typer.Exit(code=1)
    if result is None:
        typer.secho(
            f"\n{economy}: {report.n_pairs_gated} gated pairs -> 0 verified"
            f" ({report.n_no_evidence} no-evidence, {report.n_dropped} dropped)."
            f" {pdf.name} contains no provision for the requested pillar(s) that"
            " survives mechanical verification; nothing to export (an honest"
            " zero, fail-closed).",
            fg="yellow",
        )
        _print_run_record(report, pillars=run_pillars)
        raise typer.Exit(code=0)
    typer.secho(
        f"\n{economy}: {report.n_pairs_gated} gated pairs -> {report.n_passed} verified,"
        f" {report.n_no_evidence} no-evidence, {report.n_dropped} dropped,"
        f" {report.n_groups} groups -> {len(result.rows)} export rows",
        fg="green",
    )
    _print_run_record(report, pillars=run_pillars)
    _print_output_summary(result, db, out)


@app.command()
def drops(
    db: Path = typer.Option(Path("data/regcompass.db"), help="SQLite working database"),
) -> None:
    """Show what the pipeline REJECTED and why: mappings dropped by the
    mechanical verifier (verification_status='dropped'), the M6/M7 drop and
    skip decisions recorded in audit_log, the insufficient-evidence count, and
    the committed human-review drop lane (config/review_drops.json). The demo
    uses this to make the fail-closed lanes visible."""
    from regcompass.config import load_review_drops

    if not db.exists():
        typer.secho(f"No database at {db}", fg="red")
        raise typer.Exit(code=2)
    storage = Storage(db)

    dropped = storage.conn.execute(
        "SELECT document_id, indicator_id, section, verbatim_quote, uncertainty_flags"
        " FROM mappings WHERE verification_status = 'dropped'"
        " ORDER BY document_id, indicator_id"
    ).fetchall()
    typer.secho(f"Dropped mappings (verification_status='dropped'): {len(dropped)}", fg="yellow", bold=True)
    for r in dropped:
        quote = (r["verbatim_quote"] or "")[:60]
        typer.echo(
            f"  {r['document_id']}  {r['indicator_id']}  {r['section']}"
            f"  reason={r['uncertainty_flags']}  quote={quote!r}"
        )

    audit_drops = storage.conn.execute(
        "SELECT stage, decision, COUNT(*) AS n FROM audit_log"
        " WHERE stage IN ('m6_map', 'm7_verify')"
        " AND (decision LIKE 'dropped%' OR decision LIKE '%skipped%'"
        " OR decision LIKE '%DROP%' OR decision LIKE 'error:%')"
        " GROUP BY stage, decision ORDER BY stage, n DESC"
    ).fetchall()
    typer.secho("\nDrop / skip decisions in audit_log (pair id is hashed there):", fg="yellow", bold=True)
    if not audit_drops:
        typer.echo("  none")
    for r in audit_drops:
        typer.echo(f"  {r['stage']:<10} x{r['n']:<4} {r['decision']}")

    n_insufficient = storage.conn.execute(
        "SELECT COUNT(*) AS n FROM mappings WHERE insufficient_evidence = 1"
    ).fetchone()["n"]
    typer.secho(
        f"\nInsufficient-evidence records (the honest no-provision lane): {n_insufficient}",
        fg="yellow", bold=True,
    )

    review_drops = load_review_drops()
    typer.secho(
        f"\nCommitted human-review drops (config/review_drops.json): {len(review_drops)}",
        fg="yellow", bold=True,
    )
    for mapping_id, reason in review_drops.items():
        typer.echo(f"  {mapping_id}: {reason}")


@app.command()
def demo(
    host: str = typer.Option("127.0.0.1", help="Bind address"),
    port: int = typer.Option(8000, help="Port"),
    db: Path = typer.Option(Path("data/regcompass.db"), help="Working database"),
    out: Path = typer.Option(Path("out"), help="Export directory"),
    data_dir: Path = typer.Option(
        Path("data"),
        help="Root for <economy>/raw/ Corpus bytes; must match the --data-dir Discovery fetched them with",
    ),
) -> None:
    """DEPRECATED alias for `regcompass serve`."""
    typer.secho("note: `demo` is now `serve`; running that.", fg="yellow")
    serve(
        host=host, port=port, db=db, out=out, data_dir=data_dir, bundle=None,
    )


@app.command()
def gloss(
    db: Path = typer.Option(Path("data/regcompass.db"), help="SQLite working database"),
    out: Path = typer.Option(Path("out/glosses"), help="Draft-gloss artifact directory"),
    gloss_engine: str = typer.Option(
        "", "--engine",
        help="Gloss engine for this run: 'opus_mt' (offline default) or 'llm'"
             " (the configured default Engine)",
    ),
    enable: bool = typer.Option(
        False, "--enable", help="Run even though gloss_enabled is off in config/pipeline.yaml"
    ),
    include_reviewed: bool = typer.Option(
        False,
        help="Also draft records that already have a reviewed translation (comparison view)",
    ),
) -> None:
    """M3 OFFLINE draft-gloss lane (the optional path). Drafts English glosses
    for non-English verbatim snippets in the working database and writes a
    review artifact (JSON + side-by-side markdown) for a human to read. A Run
    now drafts its own Glosses into the database with the selected Engine; this
    command writes files only and changes neither the database nor the export.
    Drafts are NON-AUTHORITATIVE: a Gloss carries its label until a named person
    approves the text."""
    import json

    from regcompass.export import _verbatim_english
    from regcompass.translate import draft_glosses, make_engine, needs_gloss

    _load_dotenv()
    cfg = load_pipeline()
    if not (cfg.gloss_enabled or enable):
        typer.secho(
            "the gloss lane is switched OFF (gloss_enabled: false in"
            " config/pipeline.yaml); pass --enable to run it anyway",
            fg="red",
        )
        raise typer.Exit(code=2)
    if gloss_engine:
        if gloss_engine not in ("opus_mt", "llm"):
            typer.secho(f"unknown gloss engine '{gloss_engine}' (opus_mt or llm)", fg="red")
            raise typer.Exit(code=2)
        cfg = cfg.model_copy(update={"gloss_engine": gloss_engine})
    if not db.exists():
        typer.secho(f"No database at {db}: run `regcompass run` first", fg="red")
        raise typer.Exit(code=2)

    from regcompass.config import CONFIG_DIR
    from regcompass.map import ConfigError

    storage = Storage(db)
    try:
        records = storage.load_mappings()
        reviewed = _verbatim_english(str(CONFIG_DIR))
        todo = [
            r
            for r in records
            if needs_gloss(r.verbatim_quote)
            and (include_reviewed or r.mapping_id not in reviewed)
        ]
        if not todo:
            typer.secho("no non-English snippets need a draft gloss", fg="green")
            raise typer.Exit(code=0)
        # economy metadata beats script detection for Latin-script sources
        lang_hint = {"MY": "ms"}
        engine_impl = make_engine(
            cfg, resolve_engine(None) if cfg.gloss_engine == "llm" else None
        )
        glosses = []
        for r in todo:
            glosses.extend(
                draft_glosses(
                    [(r.mapping_id, r.verbatim_quote)],
                    engine_impl,
                    cfg,
                    source_language=lang_hint.get(r.economy),
                )
            )
    except typer.Exit:
        raise  # click's Exit subclasses RuntimeError; the green early-exit must pass through
    except ConfigError as e:
        typer.secho(str(e), fg="red")
        raise typer.Exit(code=2)
    except (RuntimeError, OSError) as e:
        typer.secho(str(e), fg="red")
        raise typer.Exit(code=1)

    out.mkdir(parents=True, exist_ok=True)
    drafted = [g for g in glosses if g.english is not None]
    payload = {
        "_comment": [
            "DRAFT glosses for human review (M3). Non-authoritative, AI-generated.",
            "A Run drafts its own Glosses into the database, where a reviewer",
            "approves one under their name and the export then ships it",
            "unlabelled. These file drafts ship nothing on their own.",
        ],
        "engine": glosses[0].engine if glosses else cfg.gloss_engine,
        "glosses": [g.model_dump() for g in glosses],
    }
    (out / "draft_glosses.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = ["# Draft glosses (M3) - review artifact", ""]
    lines += [
        "Every draft is NON-AUTHORITATIVE and AI-generated. This file ships nothing:",
        "a Gloss reaches the Evidence Export from the database, carrying its label",
        "until a named person approves the text in review.",
        "",
    ]
    for g in glosses:
        lines += [f"## `{g.key}`", ""]
        lines += [f"- engine: `{g.engine}`; source language: `{g.source_language}`"]
        if g.key in reviewed:
            lines += ["- a REVIEWED translation already ships for this record (comparison view)"]
        lines += ["", "| | text |", "|---|---|"]
        src_cell = g.source_text.replace("|", "\\|").replace("\n", " ")
        lines += [f"| source | {src_cell} |"]
        if g.english is None:
            lines += [f"| draft | (gloss failed: `{g.uncertainty_flag}`; ships without gloss) |"]
        else:
            draft_cell = g.labelled_english.replace("|", "\\|").replace("\n", " ")
            lines += [f"| draft | {draft_cell} |"]
        if g.key in reviewed:
            rev_cell = reviewed[g.key].replace("|", "\\|").replace("\n", " ")
            lines += [f"| reviewed (ships) | {rev_cell} |"]
        lines += [""]
    (out / "draft_glosses.md").write_text("\n".join(lines), encoding="utf-8")
    typer.secho(
        f"{len(drafted)} drafted, {len(glosses) - len(drafted)} fallback (no gloss)"
        f" -> {out / 'draft_glosses.json'} (+ side-by-side .md)",
        fg="green",
    )


@app.command()
def engines() -> None:
    """List the declared Engines: what each one runs, whether its weights are
    open, and whether its key is present in this environment. The key VALUE is
    never read into the output, only the variable name and whether it is set."""
    import os

    _load_dotenv()
    models = load_models()
    for name in models.engine_names:
        eng = models.engine(name)
        if eng.api_key_env is None:
            key = "key: none needed"
        elif os.environ.get(eng.api_key_env):
            key = f"key: {eng.api_key_env} set"
        else:
            key = f"key: {eng.api_key_env} NOT SET"
        default = " (default)" if name == models.default_engine else ""
        typer.echo(
            f"{name}{default}  {eng.display_name}  {eng.litellm_model}"
            f"  open weights: {'yes' if eng.open_weights else 'no'}"
            f"  concurrency: {eng.concurrency}  {key}"
        )


@app.command("check-config")
def check_config() -> None:
    """Validate every file in config/ against its contract model, and every
    rule that spans two files: each Pillar has a keyword file with a
    description and one vocabulary per Indicator, and each Indicator has a
    definition and a Pillar that lists it."""
    from regcompass.config import load_indicators, validate_config

    portals = load_portals()
    models = load_models()
    crosswalk = load_crosswalk()
    pipeline = load_pipeline()
    indicators = load_indicators()
    configured_pillars = pillars()
    for code, p in sorted(portals.items()):
        where = p.hosts[0] if p.hosts else f"no host ({p.strategy})"
        flags = ",".join(
            f for f, on in (("prepared", p.prepared), ("live-test", p.live_test_pool)) if on
        )
        typer.echo(f"economy {code}: {p.official_name} | {where} | {flags or 'configured'}")
    typer.echo(
        f"indicators: {len(indicators)} across {len(configured_pillars)} pillars"
        f" ({configured_pillars[0]} to {configured_pillars[-1]})"
    )
    engines = ", ".join(
        f"{name}={models.engine(name).litellm_model}" for name in models.engine_names
    )
    typer.echo(f"engines: {engines}")
    typer.echo(f"embedder: {models.embedder.litellm_model}")
    typer.echo(f"default engine: {models.default_engine}")
    typer.echo(f"crosswalk: emission={crosswalk.emission_scheme}")
    typer.echo(f"pipeline: extraction_attempts={pipeline.extraction_attempts} ocr_cer_max={pipeline.ocr_cer_max}")
    problems = validate_config()
    if problems:
        for problem in problems:
            typer.secho(f"  {problem}", fg="red")
        typer.secho(f"config INVALID: {len(problems)} problem(s)", fg="red")
        raise typer.Exit(code=1)
    typer.secho("config OK", fg="green")


if __name__ == "__main__":
    app()
