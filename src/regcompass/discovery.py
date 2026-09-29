"""Discovery: the only step that touches the internet.

Discovery, for one Economy, applies that Economy's configured strategy, honours
robots.txt, keeps one connection at a spacing floor under an identified user
agent, and fills the Economy's Corpus: each Document stored with its exact
bytes, its canonical text, its Source URL, its Language and its fetch time. It
writes its own Run Record carrying the count of Documents fetched.

A Run (pipeline.run_economy) then reads that Corpus and never fetches, so
"same Corpus, different Engine, zero fetches" is a fact the Run Record shows
rather than a claim anyone has to take on trust.

This module composes what already existed rather than replacing it: the
strategy dispatch and fetch machinery of crawl.py, and shortlist.ingest_economy,
which remains the one place fetched bytes become Documents.
"""

from __future__ import annotations

import time
from contextlib import nullcontext
from datetime import date
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from regcompass.config import CONFIG_DIR, load_crawl_seeds, load_portals
from regcompass.contracts import CrawlSeedsEconomy, CrawlTarget
from regcompass.crawl import (
    IMPERSONATING_METHODS,
    STRATEGIES,
    FetchResult,
    JsonGetter,
    RateLimiter,
    RobotsPolicy,
    RobotsUnavailableError,
    _default_get_json,
    crawl_economy,
    read_robots_policy,
    one_connection,
    robots_host,
    session_fetch,
    session_get_json,
)
from regcompass.discovery_progress import DiscoveryProgress, earlier_failure, skip_reason
from regcompass.storage import Storage, new_run_id, utc_now_z


class ManualEconomyError(RuntimeError):
    """Discovery does not run for this Economy, for one of two reasons the
    message tells apart: its Portal's own rules do not permit automated
    collection (manual-only, permanent, and the add-by-URL lane is closed too),
    or no Discovery strategy is configured for it yet (which may change, and
    both add lanes stay open). Either way, Documents arrive through "Add
    document"."""


@dataclass
class DiscoveryReport:
    """What one Discovery did, in the words the interface and the command line
    use. `fetched` is the number the Discovery record carries; `skipped_existing`
    is how many Documents were already in the Corpus and so were never asked
    for again."""

    economy: str
    strategy: str
    run_id: str
    refresh: bool = False
    fetched: int = 0
    skipped_existing: int = 0
    documents_stored: int = 0
    # Fetched files this Economy holds that no Corpus row carries: the count
    # that makes a Document lost between the fetch and the Corpus visible.
    # Cumulative over the Economy, not over this call, because a file that went
    # missing in an earlier Discovery is still missing.
    fetched_without_a_row: int = 0
    discovered: int = 0
    deduplicated: int = 0
    failed: int = 0
    disallowed: int = 0
    min_interval_seconds: float = 0.0
    escalated_to_impersonation: bool = False
    # Targets a Portal adapter produced whose host is not on this Economy's
    # whitelist. They are dropped before anything is queued, and counted here
    # rather than passed over in silence: a Portal that starts handing out
    # third-party download URLs is something a reviewer must see.
    skipped_off_host: int = 0
    # The status robots.txt answered when it was unavailable (5xx). Set on the
    # Discovery record so the refusal, or the decision to go ahead without the
    # rules, is checkable afterwards.
    robots_unavailable_status: int | None = None
    # The Portal's configured unavailable-robots policy, set only where it is
    # what let this Discovery past an unreadable robots.txt ('proceed'). The
    # status above says what the Portal answered; this says on whose decision
    # we went ahead, and both go onto the Discovery record.
    robots_unavailable_policy: str | None = None
    # Set when the Portal's robots.txt could not be read and either RFC 9309's
    # 30-day rule or the Portal's own policy let the Discovery run anyway. It
    # says so, in words, on the Discovery record.
    robots_note: str | None = None
    misses: list[str] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)


class _Tracker(DiscoveryProgress):
    """Passes every call on to the caller's hook and keeps what the closing
    counts need: each Document announced once, how many were skipped, and
    which were fetched in this Discovery."""

    def __init__(self, hook: DiscoveryProgress | None) -> None:
        self.hook = hook if hook is not None else DiscoveryProgress()
        self.found_urls: set[str] = set()
        self.fetched_urls: list[str] = []
        self.settled: set[str] = set()
        self.n_skipped = 0
        self.n_added = 0

    def portal(self, **fields) -> None:
        self.hook.portal(**fields)

    def found(self, url: str, name: str | None) -> None:
        if url in self.found_urls:
            return
        self.found_urls.add(url)
        self.hook.found(url, name)

    def fetched(self, url: str, size_bytes: int, method: str) -> None:
        self.fetched_urls.append(url)
        self.hook.fetched(url, size_bytes, method)

    def added(self, url, document_id, title, n_pages, ocr_applied) -> None:
        # A file an earlier Discovery fetched can be added by this one without
        # being listed today; it still counts as found.
        self.found_urls.add(url)
        self.settled.add(url)
        self.n_added += 1
        self.hook.added(url, document_id, title, n_pages, ocr_applied)

    def skipped(
        self, url: str, code: str, reason: str, title: str | None = None
    ) -> None:
        self.found_urls.add(url)
        self.settled.add(url)
        self.n_skipped += 1
        self.hook.skipped(url, code, reason, title)

    def finished(self, counts: dict) -> None:
        self.hook.finished(counts)

    def failed(self, message: str) -> None:
        self.hook.failed(message)


def _counts(report: DiscoveryReport, tracker: _Tracker) -> dict:
    """What a finished Discovery tells its hook: the numbers the screen
    shows, in the words it shows them."""
    return {
        "run_id": report.run_id,
        "found": len(tracker.found_urls),
        "fetched": report.fetched,
        # Every Document found ends added or skipped, so found is their sum.
        "added": tracker.n_added,
        "skipped": tracker.n_skipped,
        "stored": report.documents_stored,
        "already_in_corpus": report.skipped_existing,
        "failed": report.failed,
        "disallowed": report.disallowed,
        "duplicates": report.deduplicated,
        "off_whitelist": report.skipped_off_host,
        "spacing_seconds": report.min_interval_seconds,
    }


def discover_economy(
    economy: str,
    storage: Storage,
    *,
    refresh: bool = False,
    config_dir=CONFIG_DIR,
    data_dir: Path = Path("data"),
    seeds: CrawlSeedsEconomy | None = None,
    fetch: Callable[[str], FetchResult] | None = None,
    get_json: JsonGetter | None = None,
    limiter: RateLimiter | None = None,
    transport=None,
    max_documents: int | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], str] = utc_now_z,
    today: date | None = None,
    progress: Callable[[str], None] = lambda s: None,
    hook: DiscoveryProgress | None = None,
) -> DiscoveryReport:
    """Fill one Economy's Corpus from its Portal, politely, and record what that
    cost in requests.

    Without `refresh`, a Document whose Source URL is already in the Corpus is
    skipped and NO request is made for it; with `refresh`, it is re-fetched and
    the Corpus row is replaced. A Discovery that has nothing left to fetch and
    whose targets resolve without asking the Portal makes no request at all, not
    even for robots.txt.

    `fetch`, `get_json`, `limiter` and `transport` are injectable so tests drive
    Discovery over recorded Portal answers and make no live call; left alone,
    Discovery opens one connection and uses the Economy's configured strategy.
    `today` is the date RFC 9309's 30-day rule is measured against, injectable
    for the same reason: a rule keyed on dates has to be testable on a fixed
    one rather than on whatever day the suite happens to run.

    `hook` hears the Portal, each Document found, fetched, added or skipped
    (with the reason in plain words) and how the Discovery ended
    (regcompass.discovery_progress). Left None, nothing listens and the text
    lines through `progress` are the only report, exactly as before.
    """
    portals = load_portals(config_dir)
    if economy not in portals:
        raise ValueError(
            f"unknown economy '{economy}': configured economies are {sorted(portals)}"
        )
    portal = portals[economy]
    strategy = STRATEGIES[portal.strategy]
    if portal.manual_only:
        # The permanent refusal, and the reason it is permanent. It is checked
        # ahead of the strategy because it is a different statement: an Economy
        # merely waiting for a Discovery strategy of its own will lose `manual`,
        # and this one never will.
        raise ManualEconomyError(
            f"{portal.official_name} ({economy}) is manual-only: its Portal's"
            " own site rules do not permit automated collection, so Discovery"
            " must never be pointed at this Economy. Add each Document by hand"
            ' with "Add document", by uploading the file: adding it by URL'
            " would still be a request we made."
        )
    if strategy.discover is None or strategy.fetch is None:
        # NOT the same statement as manual-only. This Economy has no Discovery
        # plan wired yet, which is a fact about us; it may gain one, and its
        # add-by-URL lane stays open. config/portals.yaml carries the reason
        # per Economy, which is sometimes the Portal's own ask and sometimes
        # only that the work has not been done.
        raise ManualEconomyError(
            f"{portal.official_name} ({economy}) has no Discovery strategy"
            " configured, so Discovery fetches nothing for this Economy."
            ' Add each Document with "Add document": by its official Source'
            " URL where the Portal host is whitelisted, or by uploading the"
            " file. config/portals.yaml records why this Economy has no"
            " strategy."
        )

    # A Discovery's record is opened through the SAME Storage.run_start a Run
    # uses, with kind "discovery". It is NOT wrapped in pipeline.record_run:
    # that wrapper meters tokens and computes Engine cost, and a Discovery calls
    # no model. Its counter is documents_fetched.
    run_id = new_run_id("discovery")
    started_at = now()
    report = DiscoveryReport(
        economy=economy, strategy=portal.strategy, run_id=run_id, refresh=refresh
    )
    storage.run_start(
        run_id=run_id, kind="discovery", economy=economy, pillars=[],
        indicators=None, engine=None, started_at=started_at,
    )
    tracker = _Tracker(hook)
    tracker.portal(
        economy=economy, name=portal.official_name, hosts=list(portal.hosts),
        strategy=portal.strategy, refresh=refresh, run_id=run_id,
    )
    try:
        _discover(
            report, portal, strategy, storage,
            seeds=seeds if seeds is not None else load_crawl_seeds(config_dir)[economy],
            data_dir=Path(data_dir), config_dir=config_dir, refresh=refresh,
            fetch=fetch, get_json=get_json, limiter=limiter, transport=transport,
            max_documents=max_documents, clock=clock, sleep=sleep, today=today,
            progress=progress, tracker=tracker,
        )
    except Exception as exc:
        tracker.failed(f"{type(exc).__name__}: {exc}"[:500])
        storage.run_finish(
            run_id, status="failed", ended_at=now(),
            documents_fetched=report.fetched,
            error=f"{type(exc).__name__}: {exc}"[:500],
            details=_details(report),
        )
        raise
    storage.run_finish(
        run_id, status="completed", ended_at=now(),
        documents_fetched=report.fetched, details=_details(report),
    )
    _settle_the_rest(storage, tracker)
    tracker.finished(_counts(report, tracker))
    return report


def _details(report: DiscoveryReport) -> dict:
    """What the STORED Discovery record carries, which is what anyone reading
    the database afterwards can check.

    `escalated_to_impersonation` is here and not only on the in-memory report
    because the escalation is the one fact about a Discovery that must outlive
    the process that made it: a Portal whose published rules permit us and whose
    edge refuses us (Singapore, Indonesia) is only fetched by putting on a
    browser's identity, and a disclosure that vanishes when the command exits is
    not a disclosure. The per-Document rung is in the manifest `method` column;
    this is the Discovery-level flag."""
    details = {
        "strategy": report.strategy,
        "refresh": report.refresh,
        "skipped_existing": report.skipped_existing,
        "fetched": report.fetched,
        # Files read, then rows landed: the pair is the check, and a record
        # that carries only the first cannot be asked whether they agreed.
        "stored": report.documents_stored,
        "escalated_to_impersonation": report.escalated_to_impersonation,
    }
    if report.fetched_without_a_row:
        details["fetched_without_a_row"] = report.fetched_without_a_row
    # Written only when they happened, so an ordinary Discovery record keeps
    # the shape every reader of it already knows.
    if report.skipped_off_host:
        details["skipped_off_host"] = report.skipped_off_host
    if report.robots_unavailable_status is not None:
        details["robots_unavailable_status"] = report.robots_unavailable_status
    if report.robots_unavailable_policy:
        details["robots_unavailable_policy"] = report.robots_unavailable_policy
    if report.robots_note:
        details["robots"] = report.robots_note
    return details


def _discover(
    report: DiscoveryReport,
    portal,
    strategy,
    storage: Storage,
    *,
    seeds: CrawlSeedsEconomy,
    data_dir: Path,
    config_dir,
    refresh: bool,
    fetch,
    get_json,
    limiter,
    transport,
    max_documents,
    clock,
    sleep,
    today,
    progress,
    tracker: _Tracker,
) -> None:
    economy = report.economy
    host = robots_host(economy, config_dir)

    # One connection for the whole Discovery, unless the caller injected its
    # own fetch (a test over recorded answers, or the e2e lane).
    session = nullcontext(None) if fetch is not None else one_connection(transport=transport)
    with session as client:
        if fetch is None:
            do_fetch = session_fetch(strategy, client)
            get_json = get_json or session_get_json(client)
        else:
            do_fetch = fetch
            get_json = get_json or _default_get_json

        def watched(url: str) -> FetchResult:
            """Every fetch of this Discovery, watched for the one thing that
            must never be silent: dropping the identified user agent for a
            browser's."""
            result = do_fetch(url)
            if result.method in IMPERSONATING_METHODS:
                report.escalated_to_impersonation = True
            return result

        policy = RobotsPolicy()
        floor = max(portal.min_interval_seconds, seeds.rate_limit_seconds)

        def read_robots() -> None:
            """Read the Portal's published ask, once, before the first request
            for a Document. A Portal whose robots.txt is unavailable (5xx) stops
            the Discovery here, before any Document is requested, and the status
            it answered with goes onto the Discovery record. A Portal carrying
            the operator's `proceed` policy is the one exception, and it is not
            a quiet one: the status and the policy both go onto the record."""
            nonlocal policy, limiter
            if host is not None:
                try:
                    reading = read_robots_policy(
                        host,
                        watched,
                        unreachable_since=portal.robots_unreachable_since,
                        today=today,
                        unavailable_policy=portal.robots_unavailable_policy,
                    )
                except RobotsUnavailableError as exc:
                    report.robots_unavailable_status = exc.status
                    raise
                policy = reading.policy
                report.robots_unavailable_status = reading.unavailable_status
                report.robots_unavailable_policy = reading.unavailable_policy
                if reading.note:
                    # Either the 30-day rule or this Portal's own configured
                    # policy let the Discovery past an unreadable robots.txt.
                    # That is a decision, so it goes on the record rather than
                    # passing as an ordinary crawl.
                    report.robots_note = reading.note
                    progress(f"Discovery | {economy}: robots.txt {reading.note}")
            interval = max(floor, policy.crawl_delay or 0.0)
            report.min_interval_seconds = interval
            if limiter is None:
                limiter = RateLimiter(interval, clock=clock, sleep=sleep)
            else:
                report.min_interval_seconds = limiter.min_interval
            progress(
                f"Discovery | {economy}: spacing floor {report.min_interval_seconds}s/host,"
                f" robots disallow rules {len(policy.disallow)}"
            )

        if strategy.discovery_needs_network:
            read_robots()

        # The listing plans resolve their targets by reading the Portal's own
        # index pages, so they take the SAME watched fetch the Documents go
        # through and the SAME published policy: a listing page is a request
        # like any other, and robots binds it like any other.
        targets, report.misses = strategy.discover(
            seeds, economy, get_json=get_json, limiter=limiter,
            fetch=watched, robots=policy,
        )
        for target in targets:
            tracker.found(target.url, target.filename_hint)

        # The Portal whitelist binds what a Portal ANSWERS, not only what we
        # type. An adapter reads download URLs out of a Portal's own JSON, and
        # a Portal that started naming a third-party host would otherwise have
        # us fetch from it and file the result as official evidence. Checked
        # here, before anything is queued, so it holds for every adapter.
        if portal.hosts:
            allowed = set(portal.hosts)
            on_host: list[CrawlTarget] = []
            for target in targets:
                if urlsplit(target.url).netloc in allowed:
                    on_host.append(target)
                else:
                    report.skipped_off_host += 1
                    report.misses.append(
                        f"{economy}: {target.url} is not on the Portal"
                        " whitelist; not fetched"
                    )
                    progress(f"Discovery | off-whitelist host, skipped: {target.url}")
                    tracker.skipped(
                        target.url, "off_whitelist", skip_reason("off_whitelist")
                    )
            targets = on_host

        # Already in the Corpus: skipped without a request, unless refreshing.
        # A refresh resets EVERY target, not only the ones that reached the
        # Corpus: manifest_add_pending is INSERT OR IGNORE, so a row that failed
        # once would otherwise stay failed forever and a Portal that was down
        # for one call would be written off permanently.
        in_corpus = storage.corpus_source_urls(economy)
        if refresh:
            storage.manifest_reset_pending(t.url for t in targets)
        else:
            keep: list[CrawlTarget] = []
            for target in targets:
                if target.url in in_corpus:
                    report.skipped_existing += 1
                    tracker.skipped(
                        target.url, "in_corpus", skip_reason("in_corpus"),
                        _corpus_title(storage, target.url),
                    )
                else:
                    keep.append(target)
            targets = keep

        pending = storage.manifest_rows(economy=economy, status="pending")
        # A row left pending by an earlier call is fetched in this one, so it
        # is announced like a Document the Portal listed today.
        for row in pending:
            tracker.found(row["url"], row["filename_hint"])
        if not targets and not pending:
            progress(
                f"Discovery | {economy}: {report.skipped_existing} Document(s) already"
                " in the Corpus, nothing to fetch; no request made"
            )
            return

        if not strategy.discovery_needs_network:
            read_robots()

        # The Disallow check lives in the fetch loop, which every route to a
        # request passes, so a pending row left over from an earlier call is
        # refused too and each refusal is counted exactly once.
        crawl_report = crawl_economy(
            economy, storage, data_dir, seeds,
            fetcher=watched, get_json=get_json, limiter=limiter,
            max_documents=max_documents, targets=targets, robots=policy,
            progress=progress, hook=tracker,
        )
        report.discovered = crawl_report.discovered
        report.fetched = crawl_report.fetched
        report.deduplicated = crawl_report.deduplicated
        report.failed = crawl_report.failed
        report.disallowed = crawl_report.disallowed
        report.misses.extend(crawl_report.misses)

    # Bytes to Documents. ingest_economy is the ONE place fetched bytes become
    # Corpus rows; Discovery adds the Portal's default Language to them.
    from regcompass.shortlist import ingest_economy

    stored, excluded = ingest_economy(
        storage, data_dir, economy, language=portal.languages[0]
    )
    # Rows, not files. One result is one Corpus row, so counting the distinct
    # ids is counting what landed: a file that was read but never reached a row
    # of its own can no longer be reported as a Document we hold.
    report.documents_stored = len({r.document_id for r in stored})
    report.fetched_without_a_row = _fetched_without_a_row(storage, economy)
    report.excluded = excluded
    for item in excluded:
        report.misses.append(f"{economy}: {item['url']} not ingested ({item['reason']})")
    _report_ingest(storage, tracker, stored, excluded)
    progress(
        f"Discovery | {economy}: fetched {report.fetched}, skipped"
        f" {report.skipped_existing} already in the Corpus, stored"
        f" {report.documents_stored} Document(s) in {portal.languages[0]}"
    )
    if report.fetched_without_a_row:
        held = storage.conn.execute(
            "SELECT COUNT(*) FROM documents WHERE economy = ?", (economy,)
        ).fetchone()[0]
        progress(
            f"Discovery | {economy}: {report.fetched_without_a_row} fetched"
            f" file(s) have no Corpus row; the Corpus holds {held} Document(s)"
            f" for {economy}"
        )


def _report_ingest(storage: Storage, tracker: _Tracker, stored, excluded) -> None:
    """Tell the hook what the ingest made of each file: added as a Document,
    left out with its reason, or, for a file fetched again whose bytes the
    Corpus already holds, that nothing changed."""
    for result in stored:
        row = storage.conn.execute(
            "SELECT source_url FROM documents WHERE document_id = ?",
            (result.document_id,),
        ).fetchone()
        url = row[0] if row is not None and row[0] else result.document_id
        tracker.added(
            url, result.document_id, result.title, result.n_pages,
            bool(result.ocr_applied),
        )
    for item in excluded:
        tracker.skipped(
            item["url"], "not_added", skip_reason("not_added", item["reason"])
        )
    for url in tracker.fetched_urls:
        if url in tracker.settled:
            continue
        held = storage.conn.execute(
            "SELECT 1 FROM crawl_manifest m JOIN documents d"
            "   ON d.source_sha256 = m.sha256 WHERE m.url = ?",
            (url,),
        ).fetchone()
        if held is not None:
            tracker.skipped(url, "unchanged", skip_reason("unchanged"))


def _corpus_title(storage: Storage, url: str) -> str | None:
    row = storage.conn.execute(
        "SELECT title FROM documents WHERE source_url = ? AND title IS NOT NULL"
        " ORDER BY document_id LIMIT 1",
        (url,),
    ).fetchone()
    return row[0] if row is not None else None


def _settle_the_rest(storage: Storage, tracker: _Tracker) -> None:
    """Every Document found ends added or skipped with a reason. An address
    this Discovery found but did not ask for (it failed, was refused or was
    a duplicate on an earlier Discovery, and the manifest keeps that answer)
    is skipped here, with what its row says happened."""
    for url in sorted(tracker.found_urls - tracker.settled):
        row = storage.conn.execute(
            "SELECT status, error, is_duplicate_of, sha256 FROM crawl_manifest"
            " WHERE url = ?",
            (url,),
        ).fetchone()
        if row is None:
            tracker.skipped(url, "not_fetched", skip_reason("not_fetched"))
            continue
        status, error, duplicate_of, sha = row
        if status == "failed":
            tracker.skipped(url, "failed_before", earlier_failure(error))
        elif status == "pending":
            tracker.skipped(url, "limit", skip_reason("limit"))
        elif duplicate_of is not None:
            tracker.skipped(url, "duplicate", skip_reason("duplicate"))
        else:
            held = storage.conn.execute(
                "SELECT title FROM documents WHERE source_sha256 = ?", (sha,)
            ).fetchone()
            if held is not None:
                tracker.skipped(url, "in_corpus", skip_reason("in_corpus"), held[0])
            else:
                tracker.skipped(url, "not_added", skip_reason("not_added"))


def _fetched_without_a_row(storage: Storage, economy: str) -> int:
    """Fetched files of this Economy whose bytes carry no Corpus row.

    The ingest skips a manifest row whose digest already has a Document, so
    the digest is the honest join. Anything left over is a file we read and do
    not hold: an exclusion, or a Document that went missing on the way in."""
    return storage.conn.execute(
        "SELECT COUNT(*) FROM crawl_manifest m"
        " WHERE m.economy = ? AND m.status = 'fetched' AND m.kind = 'document'"
        "   AND m.is_duplicate_of IS NULL"
        "   AND NOT EXISTS ("
        "     SELECT 1 FROM documents d WHERE d.source_sha256 = m.sha256)",
        (economy,),
    ).fetchone()[0]
