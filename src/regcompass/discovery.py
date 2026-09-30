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

import re
import time
import unicodedata
from contextlib import nullcontext
from datetime import date
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from regcompass.config import (
    CONFIG_DIR,
    indicator_ids,
    load_baseline_laws,
    load_crawl_seeds,
    load_pipeline,
    load_portals,
)
from regcompass.contracts import (
    DISCOVERY_SOURCE_KIND,
    is_never_requested,
    CrawlFamily,
    CrawlSeedsEconomy,
    CrawlTarget,
)
from regcompass.crawl import (
    IMPERSONATING_METHODS,
    REFUSING_STATUSES,
    IN_LEGACY_HOSTS,
    IN_SEARCH,
    PATH_REFUSING_STATUSES,
    STRATEGIES,
    DeadlineReachedError,
    NotAskedError,
    bounded_session_fetch,
    HostUnreachableError,
    NeverRequestedHostError,
    RedirectOffHostError,
    follow_guarded,
    hop_for,
    in_citation_title,
    FetchFailedError,
    FetchResult,
    JsonGetter,
    LadderExhaustedError,
    RateLimiter,
    RobotsDisallowedError,
    RobotsPolicy,
    RobotsUnavailableError,
    _default_get_json,
    crawl_economy,
    read_robots_policy,
    one_connection,
    robots_host,
    robots_scheme,
    session_fetch,
    session_get_json,
)
from regcompass.discovery_progress import (
    DiscoveryProgress,
    baseline_skip_reason,
    earlier_failure,
    skip_reason,
)
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
    # Set only for a Discovery by Pillar: the draw, each Document it brought
    # in and why (baseline Indicators or the Portal crawler), each baseline
    # law it did not fetch with the reason, and plain notes on what did not
    # run (no baseline for this Economy, no crawler, no seed for the Pillar).
    pillar: int | None = None
    indicators: list[str] | None = None
    max_documents: int | None = None
    found_by: list[dict] = field(default_factory=list)
    baseline_skipped: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Per host, when a robots.txt could not be read and a rule let us go on.
    robots_notes: dict[str, str] = field(default_factory=dict)


# A host that refuses this many kinds of address (first path segments) in one
# Discovery is not asked again at all: refusals are not probed path by path.
REFUSED_KINDS_BEFORE_HOST_CLOSES = 3


def _path_key(url: str) -> tuple[str, str]:
    """(host, first path segment) of an address: the scope of a refusal."""
    parts = urlsplit(url)
    return parts.netloc, parts.path.lstrip("/").split("/", 1)[0].casefold()


@dataclass
class _Bounds:
    """What keeps a Discovery by Pillar inside the live hour, shared by both
    of its stages: one deadline, checked before every request (listing,
    robots.txt, Document), and the memo of what not to ask again. A host
    that did not answer at all is not asked again anywhere; a host that
    refused (HTTP 401, 403 or 429 on every rung) is not asked again under
    the same first path segment, because a host may challenge its search
    and detail pages and still serve its downloads. A host that refuses a
    third kind of address, or its robots.txt, is closed altogether."""

    deadline: float
    clock: Callable[[], float]
    dead: set[str] = field(default_factory=set)
    refused: set[tuple[str, str]] = field(default_factory=set)
    # Why a closed host was closed, when it answered with refusals rather
    # than not answering at all.
    why_closed: dict[str, str] = field(default_factory=dict)

    def closed(self, host: str) -> str | None:
        """Why this whole host is not asked again, or None when it is open."""
        if host in self.dead:
            return self.why_closed.get(host, f"{host} did not answer earlier; not asked again")
        return None

    def not_again(self, url: str) -> str | None:
        """Why this address is not asked, or None when it may be."""
        host, segment = _path_key(url)
        reason = self.closed(host)
        if reason:
            return reason
        if (host, segment) in self.refused:
            return f"{host} refused /{segment}/ addresses earlier; not asked again"
        return None

    def gate(self, url: str) -> None:
        """Raise before a request this Discovery must not make."""
        if self.clock() >= self.deadline:
            raise DeadlineReachedError("the Discovery time limit is reached; not asked")
        reason = self.not_again(url)
        if reason:
            raise HostUnreachableError(reason)

    def refuse(self, url: str) -> None:
        """A refusal closes its host and first path segment. A refused
        robots.txt closes the whole host, whose rules we then cannot read,
        and so does a third refused kind of address."""
        host, segment = _path_key(url)
        if segment == "robots.txt":
            self._close(host, f"{host} refused its robots.txt earlier; not asked again")
            return
        self.refused.add((host, segment))
        kinds = sum(1 for h, _ in self.refused if h == host)
        if kinds >= REFUSED_KINDS_BEFORE_HOST_CLOSES:
            self._close(
                host, f"{host} refused {kinds} kinds of addresses earlier; not asked again"
            )

    def _close(self, host: str, reason: str) -> None:
        if host not in self.dead:
            self.dead.add(host)
            self.why_closed[host] = reason

    def mark(self, url: str, exc: BaseException) -> None:
        """A request that died marks the host; a host that refused every
        rung marks the host and first path segment."""
        if isinstance(exc, (NotAskedError, NeverRequestedHostError, RedirectOffHostError)):
            return
        if isinstance(exc, LadderExhaustedError) and exc.refused:
            self.refuse(url)
        else:
            self.dead.add(urlsplit(url).netloc)

    def expired(self) -> bool:
        return self.clock() >= self.deadline

    def wrap_limiter(self, limiter):
        """The politeness wait, refused (not slept) when it would run past
        the deadline: the address stays pending."""
        return _DeadlineLimiter(limiter, self)

    def wrap(self, fetch: Callable[[str], FetchResult]) -> Callable[[str], FetchResult]:
        def bounded(url: str) -> FetchResult:
            self.gate(url)
            try:
                return fetch(url)
            except Exception as exc:
                self.mark(url, exc)
                raise

        return bounded

    def wrap_json(self, get_json: JsonGetter) -> JsonGetter:
        def bounded(url: str, params=None):
            self.gate(url)
            try:
                return get_json(url, params)
            except Exception as exc:
                self.mark(url, exc)
                raise

        return bounded


class _DeadlineLimiter:
    """A RateLimiter that will not sleep past a Discovery's deadline."""

    def __init__(self, inner: RateLimiter, bounds: _Bounds) -> None:
        self.inner = inner
        self.bounds = bounds
        self.min_interval = inner.min_interval

    def wait(self, url: str) -> None:
        if self.bounds.clock() + self.inner.remaining(url) >= self.bounds.deadline:
            raise DeadlineReachedError(
                "the Discovery time limit would pass during the politeness wait; not asked"
            )
        self.inner.wait(url)


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
        self.added_docs: list[dict] = []

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
        self.added_docs.append({"url": url, "document_id": document_id, "title": title})
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
    counts = {
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
    if report.pillar is not None:
        counts.update(_drawn_details(report))
    return counts


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
    pillar: int | None = None,
    indicators: list[str] | None = None,
    baseline_dir: Path | None = None,
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

    `pillar` (with `indicators`, or every Indicator of the Pillar when none are
    given) is a Discovery by Pillar, the live hour's: first the laws the 2025
    baseline cites for those Indicators (config/baseline_laws.json, or
    `baseline_dir`), each fetched from an official host of this Economy, then
    what the Portal crawler finds from the seeds tagged for that Pillar, with
    `max_documents` bounding both together. It runs for an Economy with no
    crawler too, as long as the baseline names an official address. Only an
    address on the Economy's allowed hosts is ever requested. Without a
    `pillar`, Discovery is exactly what it always was.
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
    drawn = pillar is not None
    has_crawler = strategy.discover is not None and strategy.fetch is not None
    if not drawn and not has_crawler:
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
    if drawn:
        report.pillar = int(pillar)
        report.indicators = list(indicators) if indicators else None
        report.max_documents = max_documents
    storage.run_start(
        run_id=run_id, kind="discovery", economy=economy,
        pillars=[report.pillar] if drawn else [],
        indicators=report.indicators, engine=None, started_at=started_at,
    )
    tracker = _Tracker(hook)
    tracker.portal(
        economy=economy, name=portal.official_name, hosts=list(portal.hosts),
        strategy=portal.strategy, refresh=refresh, run_id=run_id,
    )
    try:
        if not drawn:
            _discover(
                report, portal, strategy, storage,
                seeds=seeds if seeds is not None else load_crawl_seeds(config_dir)[economy],
                data_dir=Path(data_dir), config_dir=config_dir, refresh=refresh,
                fetch=fetch, get_json=get_json, limiter=limiter, transport=transport,
                max_documents=max_documents, clock=clock, sleep=sleep, today=today,
                progress=progress, tracker=tracker,
            )
        else:
            _discover_drawn(
                report, portal, strategy, storage, has_crawler=has_crawler,
                seeds=seeds, baseline_dir=baseline_dir,
                data_dir=Path(data_dir), config_dir=config_dir, refresh=refresh,
                fetch=fetch, get_json=get_json, limiter=limiter, transport=transport,
                clock=clock, sleep=sleep, today=today, progress=progress,
                tracker=tracker,
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
    if report.pillar is not None:
        details.update(_drawn_details(report))
    return details


def _drawn_details(report: DiscoveryReport) -> dict:
    """What a Discovery by Pillar adds to its record and its closing counts:
    the draw, why each Document came in, and each baseline law left out."""
    return {
        "pillar": report.pillar,
        "indicators": report.indicators,
        "max_documents": report.max_documents,
        "found_by": [dict(d) for d in report.found_by],
        "baseline_skipped": [dict(s) for s in report.baseline_skipped],
        "notes": list(report.notes),
        "robots_notes": dict(report.robots_notes),
    }


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
    bounds: _Bounds | None = None,
) -> None:
    economy = report.economy
    host = robots_host(economy, config_dir)

    # One connection for the whole Discovery, unless the caller injected its
    # own fetch (a test over recorded answers, or the e2e lane). A Discovery
    # by Pillar (`bounds`) asks with short, single attempts under its deadline
    # and its memo of silent hosts; any other Discovery is as it always was.
    session = (
        nullcontext(None) if fetch is not None
        else one_connection(transport=transport, timeout=BASELINE_FETCH_TIMEOUT)
        if bounds is not None
        else one_connection(transport=transport)
    )
    with session as client:
        if fetch is None and bounds is not None:
            do_fetch = bounded_session_fetch(
                strategy, client, timeout=BASELINE_FETCH_TIMEOUT, gate=bounds.gate
            )
            get_json = get_json or partial(_default_get_json, client=client, attempts=1)
        elif fetch is None:
            do_fetch = session_fetch(strategy, client)
            get_json = get_json or session_get_json(client)
        else:
            do_fetch = fetch
            get_json = get_json or _default_get_json
        if bounds is not None:
            do_fetch = bounds.wrap(do_fetch)
            get_json = bounds.wrap_json(get_json)
            if limiter is not None:
                limiter = bounds.wrap_limiter(limiter)

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
                if bounds is not None:
                    limiter = bounds.wrap_limiter(limiter)
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
            gate=bounds.gate if bounds is not None else None,
        )
        report.discovered = crawl_report.discovered
        report.fetched = crawl_report.fetched
        report.deduplicated = crawl_report.deduplicated
        report.failed = crawl_report.failed
        report.disallowed = crawl_report.disallowed
        report.misses.extend(crawl_report.misses)

    if bounds is not None and bounds.expired():
        # No reading (OCR included) starts after the deadline: the files
        # stay fetched and are read by the next Discovery or add.
        report.notes.append(
            "the time limit was reached, so files the Portal crawler fetched"
            " were not read into the Corpus"
        )
        return

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


# The Baseline Law List: for each Economy the 2025 RDTII baseline covers, the
# laws it cites per Indicator and their reference URLs, in the order to try.
BASELINE_LAWS_FILE = "baseline_laws.json"

# Where a Discovery by Pillar gets the Document cap when the caller names none.
DEFAULT_DRAWN_CAP = 12

# One short attempt per address in a Discovery by Pillar: a host that does not answer in this
# long is marked and not asked again, so a dead host costs the hour once.
BASELINE_FETCH_TIMEOUT = 25.0

CRAWLER_FOUND_BY = "portal crawler"
IN_CORPUS_STATUS = "already in the Corpus"


def _baseline_economies(config_dir, baseline_dir: Path | None) -> dict | None:
    """Economy code -> {"name", "laws": [...]} from the Baseline Law List, or
    None when the file is not installed: Discovery then runs its crawler stage
    only, and says so."""
    directory = Path(baseline_dir if baseline_dir is not None else config_dir)
    if not (directory / BASELINE_LAWS_FILE).is_file():
        return None
    return dict(load_baseline_laws(directory) or {})


def _rank_laws(laws: list[dict], drawn: list[str]) -> list[tuple[dict, list[str]]]:
    """The baseline laws citing any drawn Indicator, most useful first: the
    number of drawn Indicators cited, then a URL the baseline pairs with this
    law alone ahead of one read off a list by position, then the newest year.
    Each comes with the drawn Indicators it cites, in draw order."""
    chosen = []
    for position, law in enumerate(laws):
        cited = [i for i in drawn if i in set(law.get("indicators") or ())]
        if cited:
            chosen.append((law, cited, position))
    chosen.sort(
        key=lambda c: (
            -len(c[1]),
            # An address we seeded was checked to carry the law: tried first.
            0 if c[0].get("seeded") else 1,
            0 if c[0].get("url_pairing") == "single" else 1,
            -(c[0].get("year") or 0),
            c[2],
        )
    )
    return [(law, cited) for law, cited, _ in chosen]


# Words every law title carries, so they tell no law apart from another:
# the words for law and its kinds, and the names of the Economies.
_GENERIC_WORDS = frozenset(
    [
        "act", "acts", "law", "laws", "code", "rules", "rule", "regulation",
        "regulations", "decree", "order", "amendment", "amended", "amending",
        "the", "and", "for", "with", "from", "under", "this", "that", "into",
        "republic", "people", "peoples", "people's", "government", "national",
        "state", "federal", "federation", "kingdom", "democratic", "socialist",
        "ministry", "minister", "notice", "notification", "presidential",
        "indonesia", "china", "chinese", "india", "thailand", "thai", "lao",
        "laos", "mongolia", "russia", "russian", "viet", "vietnam", "nam",
        "kazakhstan", "singapore", "malaysia", "australia",
        "undang", "undangan", "nomor", "tahun", "peraturan", "pemerintah",
        "tentang", "закон", "федеральный", "кодекс",
    ]
)
# Chinese titles name the country first; what follows is the law's own name.
_CN_COUNTRY = "中华人民共和国"
_CJK = re.compile(r"[\u3400-\u9fff]")
_THAI_LAO = re.compile(r"[\u0e00-\u0eff]")
# The generic heads of Thai and Lao titles (Act, Royal Decree, Law on ...).
_TH_LA_GENERIC = (
    "พระราชบัญญัติ", "พระราชกำหนด", "กฎกระทรวง", "ประกาศ",
    "ກົດໝາຍວ່າດ້ວຍ", "ກົດໝາຍ", "ດຳລັດ",
)
# How far into a Document its own name is looked for, and how far its own
# title (and so its own year) runs.
_HEAD_CHARS = 5000
_TITLE_CHARS = 2000
# The Thai Buddhist Era runs 543 years ahead of the Common Era.
_BE_OFFSET = 543


def _fold(text: str) -> str:
    """One form for comparing names across scripts: NFKC, casefolded, every
    decimal digit of any script as its ASCII digit, runs of space as one."""
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    folded = "".join(
        str(unicodedata.decimal(c)) if c.isdecimal() else c for c in folded
    )
    return " ".join(folded.split())


def _numbers_in(text: str) -> set[str]:
    """Every number in the text as a whole token, leading zeros dropped:
    "28" is not in "2028", and "7" is "07"."""
    return {str(int(n)) for n in re.findall(r"(?<!\d)\d+(?!\d)", text)}


def _is_year(n: str) -> bool:
    """A Common Era or Buddhist Era year."""
    return len(n) == 4 and (1800 <= int(n) <= 2100 or 2343 <= int(n) <= 2643)


def _year_forms(n: str) -> set[str]:
    return {n, str(int(n) + _BE_OFFSET), str(int(n) - _BE_OFFSET)}


# A parenthesised abbreviation in a name, "(PDPA)", names nothing the text
# must repeat.
_ABBREVIATION = re.compile(r"\(\s*[A-Z][A-Z0-9&.\-]{1,9}\s*\)")
# The principal instrument number a name carries: "Act 504", "No. 29",
# "Nomor 11", "UU 11/2008", "Law 26/NA". Gazette and other references
# (P.U.(A) 233/94) are not it.
_PRINCIPAL = re.compile(r"(?:\bact|\bno|\bnomor|\bnumber|\buu|\blaw)\.?\s*(\d+)(?!\d)")
# A year standing on its own, not inside an identifier like C2004A01402.
_YEAR_TOKEN = re.compile(r"(?<![\w])(\d{4})(?![\w])")
# The text's own identity in the Indonesian and similar styles:
# "NOMOR 19 TAHUN 2016", "No. 10 of 2009".
_IDENTITY = re.compile(r"\b(?:nomor|no)\.?\s*(\d+)\s+(?:tahun|of)\s+(\d{4})\b")
# Phrases that put a date or year near a title without being its year.
_NOT_THE_TITLE_YEAR = (
    "registered", "version as at", "compilation", "revised edition", "reprint",
    "as at", "updated",
)
# How far after the law's name its own year is looked for.
_YEAR_WINDOW = 200


def _spelling(text: str) -> str:
    """One spelling for -ise and -ize words, so Authorisation is Authorization."""
    return re.sub(r"is(ation|e|ed|es|ing)\b", r"iz\1", text)


def _name_parts(name: str) -> tuple[set[str], set[str], set[str], set[str]]:
    """(principal numbers, years, Latin or Cyrillic words, non-spaced-script
    pieces) that could tell this law from another."""
    folded = _fold(_ABBREVIATION.sub(" ", name))
    years = {y for y in _YEAR_TOKEN.findall(folded) if _is_year(y)}
    principal = {
        str(int(n)) for n in _PRINCIPAL.findall(folded) if not (len(n) == 4 and _is_year(n))
    }
    words: set[str] = set()
    pieces: set[str] = set()
    for run in re.findall(r"[㐀-鿿]+", folded):
        core = run.replace(_CN_COUNTRY, "")
        if len(core) >= 2:
            pieces.add(core)
    for run in re.findall(r"[฀-໿]+", folded):
        core = run
        for generic in _TH_LA_GENERIC:
            core = core.replace(_fold(generic), " ")
        for part in core.split():
            if len(part) >= 4:
                pieces.update(part[i:i + 4] for i in range(len(part) - 3))
    for word in re.findall(r"[^\W\d_]+", _spelling(folded)):
        if _CJK.search(word) or _THAI_LAO.search(word):
            continue
        if len(word) >= 4 and word not in _GENERIC_WORDS:
            words.add(word)
    return principal, years, words, pieces


def _title_year(opening: str, anchors: list[str]) -> str | None:
    """The first year within a short window after the law's own name (the
    first anchor found) in the text's opening, passing over registration,
    version and edition dates. None when the name is not found there."""
    starts = [opening.find(a) for a in anchors if a and opening.find(a) >= 0]
    if not starts:
        return None
    at = min(starts)
    window = opening[at:at + _YEAR_WINDOW]
    for m in _YEAR_TOKEN.finditer(window):
        if not _is_year(m.group(1)):
            continue
        context = window[max(0, m.start() - 40):m.end() + 20]
        if any(marker in context for marker in _NOT_THE_TITLE_YEAR):
            continue
        return m.group(1)
    return None


def plausibly_the_law(name: str, text: str) -> bool:
    """Does this text plausibly carry the law of this name? Each check runs
    only where it can be made; a name with nothing distinctive left cannot
    say no, so the Document is kept.

    1. The name's years and its principal instrument number ("Act 504",
       "No. 29") appear near the start of the text as whole numbers, a year
       also counting in the Buddhist Era.
    2. Where the text states its own identity ("NOMOR 19 TAHUN 2016") and the
       name has a principal number, the first identity is this law's.
    3. The first year after the law's own name in the text's opening is one
       of the name's years: an amending law names the law it amends, but its
       own title comes first with its own year.
    4. Where the name's distinctive words are in the text's language (one of
       them appears), at least two thirds must; a Chinese, Thai or Lao title
       needs its own name in the text. An English name over text in another
       language falls back to the numbers."""
    head = _spelling(_fold(text[:_HEAD_CHARS]))
    opening = head[:_TITLE_CHARS]
    principal, years, words, pieces = _name_parts(name)
    in_head = _numbers_in(head)
    year_forms = {f for y in years for f in _year_forms(y)}
    for y in years:
        if not _year_forms(y) & in_head:
            return False
    for n in principal:
        if n not in in_head:
            return False
    if principal:
        identity = _IDENTITY.search(opening)
        if identity is not None:
            number, year = str(int(identity.group(1))), identity.group(2)
            if number not in principal or (year_forms and year not in year_forms):
                return False
    if year_forms:
        anchors = sorted(words, key=lambda w: opening.find(w) if w in opening else 10**9)
        anchors += [f"no. {n}" for n in principal] + [f"act {n}" for n in principal]
        found = _title_year(opening, anchors)
        if found is not None and found not in year_forms:
            return False
    cjk = {p for p in pieces if _CJK.search(p)}
    if cjk and _CJK.search(head) and not any(p in head for p in cjk):
        return False
    thai_lao = {p for p in pieces if _THAI_LAO.search(p)}
    if thai_lao and _THAI_LAO.search(head) and not any(p in head for p in thai_lao):
        return False
    if words:
        present = sum(1 for w in words if w in head)
        if present and present * 3 < len(words) * 2:
            return False
    return True


def _discover_drawn(
    report: DiscoveryReport,
    portal,
    strategy,
    storage: Storage,
    *,
    has_crawler: bool,
    seeds: CrawlSeedsEconomy | None,
    baseline_dir: Path | None,
    data_dir: Path,
    config_dir,
    refresh: bool,
    fetch,
    get_json,
    limiter,
    transport,
    clock,
    sleep,
    today,
    progress,
    tracker: _Tracker,
) -> None:
    """A Discovery by Pillar: the baseline stage, then the crawler stage, under
    one Document cap."""
    economy = report.economy
    cap = report.max_documents if report.max_documents is not None else DEFAULT_DRAWN_CAP
    report.max_documents = cap
    drawn = report.indicators or list(indicator_ids((report.pillar,), config_dir))
    progress(
        f"Discovery | {economy}: Pillar {report.pillar}, Indicators"
        f" {', '.join(drawn)}, at most {cap} Document(s)"
    )

    bounds = _Bounds(
        deadline=clock() + load_pipeline(config_dir).discovery_drawn_budget_seconds,
        clock=clock,
    )
    baseline = _baseline_economies(config_dir, baseline_dir)
    fetched = stored = 0
    laws: list[dict] = []
    if baseline is None:
        report.notes.append(
            "no baseline law list is installed, so Discovery ran the Portal"
            " crawler only"
        )
    elif economy not in baseline:
        report.notes.append(
            f"no 2025 baseline exists for {portal.official_name}, so there are"
            " no baseline laws to fetch"
        )
    else:
        laws = list(baseline[economy].get("laws") or [])
        if not _rank_laws(laws, drawn):
            report.notes.append(
                f"the baseline cites no law for {', '.join(drawn)} in"
                f" {portal.official_name}"
            )
    # The official addresses seeded per law (crawl_seeds.yaml `urls`), for
    # an Economy with or without a baseline and with or without a crawler.
    ranked = _rank_laws(_with_url_seeds(laws, _url_seeds(seeds, economy, config_dir)), drawn)
    if ranked:
        fetched, stored = _baseline_stage(
            report, portal, strategy, storage, ranked, cap=cap,
            data_dir=data_dir, config_dir=config_dir, refresh=refresh,
            fetch=fetch, get_json=get_json, limiter=limiter,
            transport=transport, clock=clock, sleep=sleep, today=today,
            progress=progress, tracker=tracker, bounds=bounds,
        )

    # The baseline's own counts go on the report first, so a crawler that
    # fails below cannot take them with it.
    report.fetched = fetched
    report.documents_stored = stored
    remaining = cap - fetched
    crawl_seeds = _seeds_for_pillar(
        report, portal, has_crawler, seeds, config_dir, remaining
    )
    if crawl_seeds is not None:
        mark = len(tracker.added_docs)
        report.fetched = report.documents_stored = 0
        try:
            _discover(
                report, portal, strategy, storage, seeds=crawl_seeds,
                data_dir=data_dir, config_dir=config_dir, refresh=refresh,
                fetch=fetch, get_json=get_json, limiter=limiter,
                transport=transport, max_documents=remaining, clock=clock,
                sleep=sleep, today=today, progress=progress, tracker=tracker,
                bounds=bounds,
            )
        except Exception as exc:  # noqa: BLE001 - the baseline's Documents stand
            # The laws already fetched are in the Corpus and a Run can read
            # them, so a crawler failure is said, not fatal.
            report.notes.append(
                f"the Portal crawler failed: {type(exc).__name__}: {exc}"[:300]
            )
        for doc in tracker.added_docs[mark:]:
            report.found_by.append(
                {**doc, "found_by": CRAWLER_FOUND_BY, "status": "fetched"}
            )
        report.fetched += fetched
        report.documents_stored += stored
    if not has_crawler and not report.fetched and not report.skipped_existing:
        report.notes.append(
            f"no Document was fetched for {portal.official_name}: upload the"
            ' laws by hand with "Add document"'
        )
    for note in report.notes:
        progress(f"Discovery | {economy}: {note}")
    for skip in report.baseline_skipped:
        progress(f"Discovery | {economy}: not fetched: {skip['law']}: {skip['reason']}")


def _seeds_for_pillar(
    report: DiscoveryReport, portal, has_crawler: bool, seeds, config_dir,
    remaining: int,
) -> CrawlSeedsEconomy | None:
    """The crawl seeds tagged for the drawn Pillar, or None (with a note
    saying why) when the crawler stage does not run."""
    if not has_crawler:
        report.notes.append(
            f"{portal.official_name} has no Portal crawler, so only baseline"
            " laws were fetched"
        )
        return None
    if seeds is None:
        seeds = load_crawl_seeds(config_dir).get(report.economy)
    families = {
        name: family
        for name, family in (seeds.families.items() if seeds is not None else ())
        if family.covers(report.pillar) and (family.acts or family.queries)
    }
    if not families:
        report.notes.append(
            f"no crawl seed is tagged for Pillar {report.pillar}, so the"
            " Portal crawler did not run"
        )
        return None
    if remaining <= 0:
        report.notes.append(
            f"the limit of {report.max_documents} Document(s) was reached by"
            " baseline laws, so the Portal crawler did not run"
        )
        return None
    return seeds.model_copy(update={"families": families})


def _url_seeds(seeds, economy: str, config_dir) -> list[CrawlFamily]:
    """This Economy's seed families that are fixed official addresses."""
    if seeds is None:
        seeds = load_crawl_seeds(config_dir).get(economy)
    if seeds is None:
        return []
    return [family for family in seeds.families.values() if family.urls]


# Why a law the baseline does not cite came in: an official address we seeded.
SEEDED_FOUND_BY = "official source list"


def _with_url_seeds(laws: list[dict], families: list[CrawlFamily]) -> list[dict]:
    """The baseline laws with the seeded addresses added: a seed naming a
    baseline law puts its addresses first on that law, and any other seed is
    a law of its own. Each seeded address carries its own title and Language,
    so an official English translation says so on its Document."""
    merged = [dict(law) for law in laws]
    for family in families:
        meta = {
            url: {"title": family.law, "language": family.language, "seeded": True}
            for url in family.urls
        }
        key = _fold(family.baseline_law) if family.baseline_law else None
        match = next(
            (law for law in merged if key and _fold(law.get("law") or "").startswith(key)),
            None,
        )
        if match is None:
            merged.append(
                {
                    "law": family.law, "indicators": list(family.indicators),
                    "urls": list(family.urls), "url_pairing": "single",
                    "url_meta": meta, "seeded": True, "found_by": SEEDED_FOUND_BY,
                }
            )
            continue
        match["urls"] = list(family.urls) + [
            u for u in (match.get("urls") or []) if u not in family.urls
        ]
        match["indicators"] = list(match.get("indicators") or []) + [
            i for i in family.indicators if i not in (match.get("indicators") or [])
        ]
        match["url_meta"] = {**(match.get("url_meta") or {}), **meta}
        match["seeded"] = True
    return merged


def _baseline_stage(
    report: DiscoveryReport,
    portal,
    strategy,
    storage: Storage,
    ranked: list[tuple[dict, list[str]]],
    *,
    cap: int,
    data_dir: Path,
    config_dir,
    refresh: bool,
    fetch,
    get_json,
    limiter,
    transport,
    clock,
    sleep,
    today,
    progress,
    tracker: _Tracker,
    bounds: _Bounds,
) -> tuple[int, int]:
    """Fetch each ranked baseline law from the first of its addresses on an
    official host of this Economy that answers with law text, through the
    same lane "Add document" by URL takes. Returns (fetched, stored).

    Bounded three ways, because the live hour is: one short attempt per
    address, a host that failed to answer is not asked again, and the stage
    stops at a time budget and at twice the Document cap in addresses tried.
    Redirects are followed by hand and only to another allowed host."""
    from regcompass.corpus import add_document_from_url

    economy = report.economy
    allowed = frozenset(h for h in portal.hosts if not is_never_requested(h))
    portal_host = portal.hosts[0] if portal.hosts else None
    title_search = portal.strategy == "dspace_rest"
    budget = load_pipeline(config_dir).discovery_baseline_budget_seconds
    max_tries = 2 * cap
    started = clock()
    # Its own sub-budget, inside the Discovery's shared deadline.
    stage_deadline = min(started + budget, bounds.deadline)
    fetched = stored = tries = 0
    robots: dict[tuple[str, str], RobotsPolicy | Exception] = {}
    limiters: dict[str, RateLimiter] = {}
    dead = bounds.dead

    def skip(law: dict, cited: list[str], code: str, detail: str | None = None) -> None:
        report.baseline_skipped.append(
            {
                "law": law.get("law") or "",
                "indicators": cited,
                "urls": list(law.get("urls") or []),
                "code": code,
                "reason": baseline_skip_reason(code, detail),
            }
        )

    # A Portal whose answers are slow but whole says so in its entry.
    timeout = portal.fetch_timeout_seconds or BASELINE_FETCH_TIMEOUT
    session = (
        nullcontext(None) if fetch is not None
        else one_connection(timeout=timeout, transport=transport)
    )
    with session as client:
        if fetch is None:
            portal_hop = hop_for(portal.strategy, client=client, timeout=timeout)
            plain_hop = hop_for("httpx", client=client, timeout=timeout)
            # One attempt, like every request of a Discovery by Pillar.
            json_getter = get_json or partial(_default_get_json, client=client, attempts=1)
        else:
            portal_hop = plain_hop = fetch
            json_getter = get_json or _default_get_json

        def hop(url: str) -> FetchResult:
            """One request, and the memo of what not to ask again: a
            transport failure marks the host, a refusal the host and first
            path segment."""
            host = urlsplit(url).netloc
            reason = bounds.not_again(url)
            if reason:
                raise HostUnreachableError(reason)
            if clock() >= bounds.deadline:
                raise DeadlineReachedError("the Discovery time limit is reached; not asked")
            try:
                result = (portal_hop if host == portal_host else plain_hop)(url)
            except (NeverRequestedHostError, RedirectOffHostError):
                raise
            except Exception as exc:
                bounds.mark(url, exc)
                raise
            if result.method in IMPERSONATING_METHODS:
                report.escalated_to_impersonation = True
                if result.http_status in PATH_REFUSING_STATUSES:
                    # Refused as ourselves and refused again impersonating:
                    # these pages of this host will not serve us today.
                    bounds.refuse(url)
                elif result.http_status in REFUSING_STATUSES:
                    dead.add(host)
            if not url.endswith("/robots.txt") and result.http_status < 400 and not result.location:
                tracker.fetched(url, len(result.content), result.method)
            return result

        def guarded(url: str) -> FetchResult:
            result = follow_guarded(url, hop, allowed=allowed)
            if not url.endswith("/robots.txt") and bounds.expired():
                # Fetched as the time ran out: not read (no OCR starts after
                # the deadline), so the law is listed for the operator.
                raise DeadlineReachedError(
                    "the Discovery time limit was reached before this law could be read"
                )
            return result

        def policy_for(host: str, scheme: str = "https") -> RobotsPolicy:
            """The host's published rules, read once per Discovery. The
            Portal's own robots decisions (the 30-day rule, `proceed`) bind
            its own host only; every other host gets the default refusal.
            A host the Portal lists under `http_hosts` is read over http
            when the address is http, and the record says so."""
            if (scheme, host) not in robots:
                own = host == portal_host
                if scheme == "http":
                    report.notes.append(
                        f"{host} was asked over plain http, which its Portal"
                        " entry allows for this host: no TLS protected the"
                        " text in transit"
                    )
                try:
                    reading = read_robots_policy(
                        host, guarded,
                        unreachable_since=portal.robots_unreachable_since if own else None,
                        today=today,
                        unavailable_policy=portal.robots_unavailable_policy if own else "refuse",
                        scheme=scheme,
                    )
                    robots[(scheme, host)] = reading.policy
                    if reading.note:
                        report.robots_notes[host] = reading.note
                        if own:
                            report.robots_note = reading.note
                            report.robots_unavailable_status = reading.unavailable_status
                            report.robots_unavailable_policy = reading.unavailable_policy
                except Exception as exc:  # noqa: BLE001 - recorded per law
                    robots[(scheme, host)] = exc
            answer = robots[(scheme, host)]
            if isinstance(answer, Exception):
                raise answer
            closed = bounds.closed(host)
            if closed:
                # robots.txt did not answer at the transport level either,
                # or the host refused it.
                raise HostUnreachableError(closed)
            return answer

        def limiter_for(host: str, policy: RobotsPolicy) -> RateLimiter:
            if limiter is not None:
                return bounds.wrap_limiter(limiter)
            if host not in limiters:
                floor = max(portal.min_interval_seconds, policy.crawl_delay or 0.0)
                limiters[host] = bounds.wrap_limiter(
                    RateLimiter(floor, clock=clock, sleep=sleep)
                )
            report.min_interval_seconds = max(
                report.min_interval_seconds, limiters[host].min_interval
            )
            return limiters[host]

        json_getter = bounds.wrap_json(json_getter)

        def title_search_urls(law: dict) -> tuple[list[str], tuple[str, str] | None]:
            """India Code's own title search, for a law the baseline gives no
            official address for: only an exact title match counts. Returns
            the addresses and, when there are none, (reason code, detail):
            the search failing or answering an error is `unreachable`, a
            search that answered with no law of exactly this title is
            `no_title_match`."""
            query = CrawlSeedsEconomy(
                rate_limit_seconds=max(portal.min_interval_seconds, 0.01),
                families={
                    "baseline": CrawlFamily(queries=[in_citation_title(law.get("law") or "")])
                },
            )
            host = urlsplit(IN_SEARCH).netloc
            try:
                policy = policy_for(host)
                targets, misses = strategy.discover(
                    query, economy, get_json=json_getter,
                    limiter=limiter_for(host, policy), fetch=guarded, robots=policy,
                )
            except DeadlineReachedError:
                return [], ("time_limit", None)
            except Exception as exc:  # noqa: BLE001 - reported as unreachable
                return [], (
                    "unreachable",
                    f"the India Code title search failed: {type(exc).__name__}: {exc}"[:200],
                )
            urls = [t.url for t in targets if urlsplit(t.url).netloc in allowed]
            if urls:
                return urls, None
            status = next(
                (m.group(1) for miss in misses
                 for m in [re.search(r"\(HTTP (\d+)\)", miss)] if m),
                None,
            )
            if status is not None and status != "200":
                return [], (
                    "unreachable", f"the India Code title search answered HTTP {status}"
                )
            return [], ("no_title_match", None)

        in_corpus = storage.corpus_source_urls(economy)

        def already_held(urls: list[str], name: str, found_by: str) -> bool:
            """A law already in the Corpus under one of these addresses is
            reported as held and not fetched again, unless refreshing."""
            held = next((u for u in urls if u in in_corpus), None)
            if held is None or refresh:
                return False
            report.skipped_existing += 1
            title = _corpus_title(storage, held)
            tracker.skipped(held, "in_corpus", skip_reason("in_corpus"), title)
            report.found_by.append(
                {
                    "url": held, "document_id": _corpus_id(storage, held),
                    "title": title or name, "found_by": found_by,
                    "status": IN_CORPUS_STATUS,
                }
            )
            return True

        for law, cited in ranked:
            name = law.get("law") or ""
            urls = [
                u for u in (law.get("urls") or [])
                if isinstance(u, str) and u and not _is_home_page(u)
            ]
            # A Portal's catalogue page describes the law without its text:
            # never fetched, so a law with no other address is an honest gap.
            summaries = [u for u in urls if _is_summary_page(u)]
            urls = [u for u in urls if u not in summaries]
            usable = [u for u in urls if urlsplit(u).netloc in allowed]
            # A link on the old India Code host is looked up by title on the
            # new one instead of being fetched: the old host refuses.
            legacy = [
                u for u in usable
                if title_search and urlsplit(u).netloc in IN_LEGACY_HOSTS
            ]
            if legacy and (refresh or not any(u in in_corpus for u in legacy)):
                usable = [u for u in usable if u not in legacy]
            else:
                legacy = []
            search_error = None
            # At most one title search per law: here, or as the fallback below.
            searched = bool((not usable or legacy) and title_search and name)
            if (not usable or legacy) and title_search and name:
                if fetched >= cap:
                    skip(law, cited, "over_cap", str(cap))
                    continue
                if clock() >= stage_deadline:
                    skip(law, cited, "time_limit", _minutes(budget))
                    continue
                found_urls, search_error = title_search_urls(law)
                usable = found_urls + usable
            if not usable:
                if search_error:
                    skip(law, cited, *search_error)
                elif summaries:
                    skip(law, cited, "summary_page")
                elif not urls:
                    skip(law, cited, "no_url")
                else:
                    hosts = sorted({urlsplit(u).netloc for u in urls})
                    skip(law, cited, "not_allowed_host", ", ".join(hosts))
                continue
            found_by = f"{law.get('found_by') or 'baseline'} {', '.join(cited)}"
            if already_held(usable, name, found_by):
                continue
            if fetched >= cap:
                skip(law, cited, "over_cap", str(cap))
                continue
            failure: tuple[str, str | None] | None = None
            # Every link that failed, and why: when all of them were refused
            # or did not answer, India Code is searched by title once.
            codes: list[str] = []
            attempted: set[str] = set()
            rounds = [usable]
            done = False
            for round_urls in rounds:
                for url in round_urls:
                    if clock() >= stage_deadline:
                        failure = ("time_limit", _minutes(budget))
                        break
                    if tries >= max_tries:
                        failure = ("attempt_limit", str(max_tries))
                        break
                    host = urlsplit(url).netloc
                    reason = bounds.not_again(url)
                    if reason:
                        failure = ("unreachable", reason)
                        codes.append(failure[0])
                        continue
                    tries += 1
                    attempted.add(url)
                    meta = (law.get("url_meta") or {}).get(url) or {}
                    title = meta.get("title") or name or None
                    tracker.found(url, title)
                    try:
                        policy = policy_for(host, robots_scheme(url, portal.http_hosts))
                        added = add_document_from_url(
                            storage, data_dir, economy, url, title=title,
                            language=meta.get("language"),
                            source_kind=DISCOVERY_SOURCE_KIND, config_dir=config_dir,
                            fetch=guarded, limiter=limiter_for(host, policy),
                            robots=policy,
                        )
                    except Exception as exc:  # noqa: BLE001 - classified, then reported
                        failure = _classify_failure(exc)
                        codes.append(failure[0])
                        progress(f"Discovery | {economy}: {url}: {failure[1]}")
                        tracker.skipped(
                            url, "not_added" if failure[0] == "no_law_text" else "fetch_error",
                            baseline_skip_reason(*failure), name,
                        )
                        continue
                    if law.get("url_pairing") == "positional" and not meta.get("seeded"):
                        # The text, not the title: the title is the name we gave it.
                        text = storage.document_full_text(added.document_id)
                        if not plausibly_the_law(name, text):
                            # Paired with this law by position in a row that cites
                            # several: the address carries another law. Not kept.
                            storage.remove_document(added.document_id, data_dir=data_dir)
                            failure = ("wrong_law", None)
                            codes.append(failure[0])
                            progress(
                                f"Discovery | {economy}: {url}: not {name}, discarded"
                            )
                            tracker.skipped(
                                url, "not_added", baseline_skip_reason("wrong_law"), name
                            )
                            continue
                    failure = None
                    fetched += 1
                    stored += 1
                    tracker.added(
                        url, added.document_id, added.title, added.n_pages,
                        bool(added.ocr_applied),
                    )
                    in_corpus.add(url)
                    report.found_by.append(
                        {
                            "url": url, "document_id": added.document_id,
                            "title": added.title, "found_by": found_by,
                            "status": "fetched",
                        }
                    )
                    progress(f"Discovery | {economy}: {found_by}: {added.title}")
                    done = True
                    break
                if done or searched or not (title_search and name):
                    break
                if not codes or any(c != "unreachable" for c in codes):
                    break
                if fetched >= cap or clock() >= stage_deadline or tries >= max_tries:
                    break
                # Every link was refused or did not answer: the law may still
                # be on India Code under its own title.
                searched = True
                found_urls, _ = title_search_urls(law)
                found_urls = [u for u in found_urls if u not in attempted]
                if already_held(found_urls, name, found_by):
                    failure = None
                    break
                if found_urls:
                    rounds.append(found_urls)
            if failure is not None:
                skip(law, cited, *failure)
    return fetched, stored


def _is_home_page(url: str) -> bool:
    """A site's front page (no path, no query) is no law's address, though
    the 2025 baseline gives a few laws one; fetched, the page would be filed
    under the law's name."""
    parts = urlsplit(url)
    return parts.path in ("", "/") and not parts.query


# Catalogue pages of an official Portal: the law's metadata and an abstract,
# not its articles. A Verbatim Quote from one would be the Portal's summary.
_SUMMARY_PAGES = {
    "peraturan.bpk.go.id": ("/Details/", "/Home/Details/"),
    "jdih.komdigi.go.id": ("/produk_hukum/view/",),
    "jdih.kominfo.go.id": ("/produk_hukum/view/",),
}


def _is_summary_page(url: str) -> bool:
    parts = urlsplit(url)
    prefixes = _SUMMARY_PAGES.get(parts.netloc.lower())
    return bool(prefixes) and parts.path.startswith(prefixes)


def _minutes(seconds: float) -> str:
    minutes = seconds / 60
    return f"{minutes:g} minute{'' if minutes == 1 else 's'}"


def _classify_failure(exc: Exception) -> tuple[str, str]:
    """Why one baseline address gave no Document, as (code, detail)."""
    from regcompass.corpus import (
        DuplicateDocumentError,
        TooLittleTextError,
        UnreadableFileError,
    )

    detail = f"{exc}"[:200] or type(exc).__name__
    if isinstance(exc, (RedirectOffHostError, NeverRequestedHostError)):
        return "redirected_off", detail
    if isinstance(exc, DeadlineReachedError):
        return "time_limit", None
    if isinstance(exc, (RobotsDisallowedError, RobotsUnavailableError)):
        return "robots", detail
    if isinstance(exc, (TooLittleTextError, UnreadableFileError)):
        return "no_law_text", detail
    if isinstance(exc, (FetchFailedError, LadderExhaustedError, HostUnreachableError)):
        return "unreachable", detail
    if isinstance(exc, DuplicateDocumentError):
        return "duplicate", detail
    if type(exc) is RuntimeError:
        # The ingest read the file and made no Document of it.
        return "no_law_text", detail
    if isinstance(exc, ValueError):
        return "not_added", detail
    return "unreachable", f"{type(exc).__name__}: {detail}"


def _corpus_id(storage: Storage, url: str) -> str | None:
    row = storage.conn.execute(
        "SELECT document_id FROM documents WHERE source_url = ?"
        " ORDER BY document_id LIMIT 1",
        (url,),
    ).fetchone()
    return row[0] if row is not None else None


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
