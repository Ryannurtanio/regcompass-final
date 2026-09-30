"""M10 crawl: fetch the real per-economy corpus as EXACT bytes, with a SQLite
manifest (url, status, sha256, fetched_at, local_path) as the checkpoint/resume
source of truth.

Stored bytes are the server response body verbatim: streamed bytes for PDFs,
the raw HTML response for act pages. Never a serialized DOM, never a re-flow.
These bytes are what M1 hashes (ground-truth definition).

Politeness is not a footnote here. Every request goes out under an IDENTIFIED
user agent naming the project and a contact URL; robots.txt is read for our own
agent (crawl-delay AND Disallow); one connection is reused for one Discovery at
a spacing floor of max(portal floor, seed floor, published crawl-delay). The
browser-impersonating rungs exist only as the documented escalation after a
Portal refuses the identified agent, and a Discovery that used one says so.

Per-economy strategy:
- SG (sso.agc.gov.sg): escalation ladder, politest rung first, each rung smoke-
  tested never assumed: plain httpx as ourselves -> curl_cffi TLS impersonation
  (clears the WAF today) -> plain headless Playwright (response.body() is still
  the raw server bytes). Patchright headful is the documented rung 4, wired only
  if the first three die.
- AU (legislation.gov.au): the SPA's own public OData API over plain httpx:
  title search -> versions/find(titleId,'latest')?$expand=documents -> the
  deterministic download URL /{titleId}/{start}/{retro}/text/original/pdf/{vol}
  (discovered by sniffing the SPA's XHR).
- MY (lom.agc.gov.my): the portal's fess search proxy (fess-proxy.php) returns
  Solr JSON whose docs carry {path, docName} -> direct PDF URLs under
  /ilims/upload/portal/akta/.
- LA (laoofficialgazette.gov.la): the Official Gazette's own legislation grid,
  filtered by a Lao word per source family (Document[title]) and paged by
  Document_page; each row carries the official Lao PDF and, for a few
  instruments, an English rendering whose URL becomes a note and never a Source
  URL. The host answers /robots.txt with its homepage, which publishes no rules.
- ID (peraturan.bpk.go.id): the same ladder as SG. Its robots.txt permits every
  route we take and its Cloudflare edge still refuses the identified agent, so
  the escalation is the only way in and the Discovery record says it was used.
  Seeds are the Portal's own /Download/{file id}/{file name} references, which
  cannot be computed from a /Details/ id.

Discovery is seeded by SOURCE FAMILIES (config/crawl_seeds.yaml), never by
feeding indicator questions to a search engine.

docs/PORTALS.md is the per-portal adapter reference: every endpoint, URL shape,
and verified quirk (AU volume-0 downloads, title-ID vs register-ID, the MY
doubled-path bug and anchor-first rule, kategori scope, SG WAF behavior), with
verification dates. Read it before touching a portal adapter or adding one.

Politeness is a scored capability: at run time each economy's robots.txt is
fetched through the same strategy the crawl uses, and the per-host rate
limiter enforces max(config floor, published crawl-delay) - SG publishes 6s,
AU 10s, MY serves no robots.txt (verified 6 Jul 2026). Transport retries are
tenacity-capped so a dead URL is recorded as failed, not hammered.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import partial
from pathlib import Path
from typing import Callable
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from regcompass import __version__
from regcompass.contracts import (
    CONNECTIONS_PER_HOST,
    MANUAL_STRATEGY,
    is_never_requested,
    ROBOTS_UNREACHABLE_GRACE_DAYS,
    CrawlSeedsEconomy,
    CrawlTarget,
)
from regcompass.discovery_progress import DiscoveryProgress, skip_reason
from regcompass.observability import log_stage
from regcompass.paths import storable_local_path
from regcompass.storage import Storage

# WHO WE ARE. Every Discovery request goes out under this, so a Portal
# administrator reading their logs can see us and reach us. It is also the
# agent token robots.txt groups are matched against.
USER_AGENT_TOKEN = "RegCompass"
IDENTIFIED_USER_AGENT = (
    f"{USER_AGENT_TOKEN}/{__version__}"
    " (+https://github.com/Ryannurtanio/regcompass)"
)

# The browser string the impersonating rungs wear. It is NOT the opening move:
# a Discovery always asks first as itself, and only escalates here after the
# Portal refuses the identified agent (403 or a WAF challenge). Kept because
# Singapore's CloudFront refuses everything else; the escalation is recorded in
# the Discovery report so nothing about it is silent.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
USER_AGENT = BROWSER_USER_AGENT  # backwards-compatible alias

# Fetch methods that wear a browser's identity rather than ours.
IMPERSONATING_METHODS = frozenset({"curl_cffi", "playwright"})

# WAF / throttle answers that justify escalating to the next ladder rung.
# A 404 is NOT here: it means the same thing on every rung and is final.
_BLOCK_STATUSES = frozenset({401, 403, 429, 503})
REFUSING_STATUSES = _BLOCK_STATUSES
# Refusals a host gives one kind of page and not another (a bot challenge on
# its search and detail pages while its downloads answer): a Discovery keeps
# these per host and first path segment. A 503, like a request that got no
# answer, still closes the whole host for that Discovery.
PATH_REFUSING_STATUSES = frozenset({401, 403, 429})

_TRANSPORT_ATTEMPTS = 3  # per-fetch transport retry cap (tenacity)

AU_API = "https://api.prod.legislation.gov.au/v1"
MY_FESS = "https://lom.agc.gov.my/fess-proxy.php"
MY_ILIMS = "https://lom.agc.gov.my/ilims"
SG_ACT = "https://sso.agc.gov.sg/Act/{code}"
# India Code's public read-only REST API. The Portal MOVED: www.indiacode.nic.in
# now serves a migration notice (recorded 16 Sep 2026) pointing at this host,
# which runs DSpace 7 and publishes this API for reading.
IN_API = "https://indiacode.gov.in/server/api"
IN_SEARCH = f"{IN_API}/discover/search/objects"
# The old India Code hosts. They now refuse (HTTP 403), so a link on them is
# looked up by title on the Portal's new host instead of being fetched.
IN_LEGACY_HOSTS = frozenset({"www.indiacode.nic.in", "indiacode.nic.in"})
# Indonesia: the Audit Board's national regulation database. A statute's own PDF
# lives under /Download/{file id}/{file name}, and the file id is NOT the id in
# the /Details/ page URL, so a Document URL cannot be computed from a detail id.
# The seeds carry the download reference read off the Portal's own search
# listing; the recorded listings are committed beside them
# (tests/fixtures/portals/id/). See docs/PORTALS.md.
ID_DOWNLOAD = "https://peraturan.bpk.go.id/Download/{ref}"


def robots_host(economy: str, config_dir=None) -> str | None:
    """The document host we actually load for this Economy, and therefore the
    host whose robots.txt binds us: the FIRST entry of its Portal whitelist.
    None when the Economy has no whitelisted host (a manual-acquisition Economy
    crawls nothing, so there is no robots ask to honour).

    SG publishes crawl-delay: 6 (verified 6 Jul 2026 via curl_cffi; a plain
    fetch gets the CloudFront 403, which is how an earlier note wrongly
    recorded "publishes none"). AU publishes 10; MY's robots.txt answers
    HTTP 500 (same date), which is "unavailable" rather than "no rules" and so
    refuses a crawl today (see _robots_text and docs/PORTALS.md)."""
    from regcompass.config import CONFIG_DIR, load_portals

    portals = load_portals(config_dir or CONFIG_DIR)
    portal = portals.get(economy)
    return portal.hosts[0] if portal and portal.hosts else None


class LadderExhaustedError(RuntimeError):
    """Every rung of the escalation ladder was blocked or died. `statuses`
    holds each rung's refusing HTTP status, or None for a rung that died."""

    def __init__(self, message: str, statuses: tuple = ()):
        super().__init__(message)
        self.statuses = tuple(statuses)

    @property
    def refused(self) -> bool:
        """Every rung answered, each with a refusal of this kind of page."""
        return bool(self.statuses) and all(
            s in PATH_REFUSING_STATUSES for s in self.statuses
        )


class DiscoveryError(RuntimeError):
    """A seed could not be resolved to document URLs."""


@dataclass
class FetchResult:
    """One fetched URL: exact response body bytes plus transport facts."""

    url: str
    final_url: str
    http_status: int
    content: bytes
    content_type: str | None
    method: str  # httpx | curl_cffi | playwright
    # Where a redirect answer points, for a fetch that does not follow it
    # itself (fetch_hop). None for every other answer.
    location: str | None = None


# How many redirects a fetch that follows them by hand will take.
MAX_REDIRECTS = 5


class NeverRequestedHostError(RuntimeError):
    """A request was about to go to a host we never ask. Raised BEFORE it is
    sent, by every client this module opens."""


class RedirectOffHostError(RuntimeError):
    """A redirect pointed off the hosts this fetch may ask, so it was not
    followed and the address it named was never requested."""

    def __init__(self, url: str, target: str):
        self.url = url
        self.target = target
        super().__init__(
            f"{url} redirects to {urlsplit(target).netloc}, which is not an"
            " official host for this Economy; not followed"
        )


class NotAskedError(RuntimeError):
    """A request that was not made, by a Discovery's own rule rather than the
    Portal's answer. The fetch loop leaves the row pending for a later call
    instead of settling it as failed."""


class HostUnreachableError(NotAskedError):
    """This host already failed to answer in this Discovery, so it is not
    asked again."""


class DeadlineReachedError(NotAskedError):
    """The Discovery's time budget is spent, so nothing more is asked."""


def _is_timeout(exc: BaseException) -> bool:
    """A request that ran out of time, on any rung's client library."""
    return isinstance(exc, (httpx.TimeoutException, TimeoutError)) or (
        "timeout" in type(exc).__name__.lower() or "timed out" in str(exc).lower()
    )


def _host(url: str) -> str:
    return (urlsplit(url).hostname or urlsplit(url).netloc or "").lower()


def refuse_never_requested(url: str) -> None:
    """Raise before any request to a host on the never-requested list."""
    if is_never_requested(_host(url)):
        raise NeverRequestedHostError(
            f"{_host(url)} is never requested; {url} was not asked for"
        )


def _guard_request(request: httpx.Request) -> None:
    """httpx request hook: fires for every request a client sends, redirects
    included, so a redirect to a never-requested host is refused unsent."""
    refuse_never_requested(str(request.url))


_GUARD_HOOKS = {"request": [_guard_request]}


def fetch_hop(
    url: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 25.0,
    transport: httpx.BaseTransport | None = None,
) -> FetchResult:
    """ONE request under the identified user agent, ONE attempt, redirects
    NOT followed: a 3xx comes back with its `location`, so the caller decides
    whether the next address may be asked at all (follow_guarded)."""
    refuse_never_requested(url)
    if client is not None:
        r = client.get(url, follow_redirects=False)
    else:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": IDENTIFIED_USER_AGENT},
            transport=transport,
            event_hooks=_GUARD_HOOKS,
        ) as c:
            r = c.get(url)
    location = r.headers.get("location") if r.is_redirect else None
    return FetchResult(
        url=url,
        final_url=str(r.url),
        http_status=r.status_code,
        content=r.content,
        content_type=r.headers.get("content-type"),
        method="httpx",
        location=urljoin(url, location) if location else None,
    )


def fetch_curl_cffi_hop(url: str, *, timeout: float = 25.0) -> FetchResult:
    """The impersonating rung as ONE request with redirects not followed, for
    the lanes that follow redirects by hand (follow_guarded)."""
    refuse_never_requested(url)
    from curl_cffi import requests as cffi_requests

    r = cffi_requests.get(url, impersonate="chrome", timeout=timeout, allow_redirects=False)
    location = r.headers.get("location") if 300 <= r.status_code < 400 else None
    return FetchResult(
        url=url, final_url=str(r.url), http_status=r.status_code, content=r.content,
        content_type=r.headers.get("content-type"), method="curl_cffi",
        location=urljoin(url, location) if location else None,
    )


def hop_for(
    strategy_name: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 25.0,
) -> Callable[[str], FetchResult]:
    """One-request fetch for a lane that follows redirects by hand: the
    identified httpx request, and for an Economy on the escalation ladder the
    impersonating curl_cffi request after a refusal (never a browser, which
    would follow redirects itself)."""

    def hop(url: str) -> FetchResult:
        fr = fetch_hop(url, client=client, timeout=timeout)
        if strategy_name == "curl_cffi_ladder" and fr.http_status in _BLOCK_STATUSES:
            try:
                return fetch_curl_cffi_hop(url, timeout=timeout)
            except ImportError:
                return fr
        return fr

    return hop


def follow_guarded(
    url: str,
    hop: Callable[[str], FetchResult],
    *,
    allowed: set[str] | frozenset[str] | None,
    max_redirects: int = MAX_REDIRECTS,
) -> FetchResult:
    """Fetch `url` with `hop` (one request, redirects not followed), following
    each redirect by hand. Every address is checked BEFORE it is requested:
    a never-requested host is refused always, and with `allowed` given, a
    redirect to any host outside it is refused (RedirectOffHostError)."""
    current = url
    for _ in range(max_redirects + 1):
        refuse_never_requested(current)
        if current != url and allowed is not None and _host(current) not in allowed:
            raise RedirectOffHostError(url, current)
        fr = hop(current)
        location = getattr(fr, "location", None)
        if 300 <= fr.http_status < 400 and location:
            nxt = urljoin(current, location)
            if is_never_requested(_host(nxt)):
                raise RedirectOffHostError(url, nxt)
            current = nxt
            continue
        return FetchResult(
            url=url, final_url=current, http_status=fr.http_status,
            content=fr.content, content_type=fr.content_type, method=fr.method,
        )
    raise FetchFailedError(f"more than {max_redirects} redirects from {url}")


# The error a manifest row carries when robots.txt refused it. A refusal is not
# a failure of ours, but it settles the row the same way: recorded, never
# retried. It reuses the `failed` status rather than adding a fourth one so the
# CHECK constraint on databases created before this change keeps accepting
# writes; the prefix is what tells the two apart.
ROBOTS_DISALLOWED_ERROR = "robots.txt disallows this URL for our user agent"


class RobotsDisallowedError(RuntimeError):
    """The Portal's published rules refuse this URL to our user agent. Raised
    by the single-URL lane (fetch_one), where there is no manifest row to
    settle and the caller is a person waiting for an answer."""


class FetchFailedError(RuntimeError):
    """The Portal answered, and the answer was not a document (an HTTP error
    status). Separate from a transport failure, which surfaces as itself."""


@dataclass
class CrawlReport:
    """What one crawl_economy run did (counts over manifest transitions)."""

    economy: str
    discovered: int = 0
    fetched: int = 0
    deduplicated: int = 0
    failed: int = 0
    disallowed: int = 0  # refused by robots.txt, never requested
    skipped_resume: int = 0
    misses: list[str] = field(default_factory=list)  # seeds that resolved to nothing


class RateLimiter:
    """Per-host politeness floor: at least min_interval seconds between two
    requests to the same host. Injectable clock/sleep for tests."""

    def __init__(
        self,
        min_interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last: dict[str, float] = {}

    def remaining(self, url: str) -> float:
        """How long wait(url) would sleep now."""
        last = self._last.get(urlsplit(url).netloc)
        return 0.0 if last is None else max(0.0, self.min_interval - (self._clock() - last))

    def wait(self, url: str) -> None:
        host = urlsplit(url).netloc
        last = self._last.get(host)
        if last is not None:
            remaining = self.min_interval - (self._clock() - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last[host] = self._clock()


# A body that opens as a web page is not a robots file. Some Portals answer
# /robots.txt with 200 and their own homepage (a soft 404: Lao PDR does,
# verified 16 Sep 2026), and reading that markup as rules would be inventing
# both permissions and refusals out of HTML.
_HTML_BODY_RE = re.compile(r"<\s*(?:!doctype\s+html|html\b|head\b|body\b)", re.IGNORECASE)


def is_html_body(text: str) -> bool:
    """True when this body is a web page rather than a robots.txt file. Only
    the opening of the body is examined: a real robots.txt may legitimately
    mention an HTML path, and it will never START as markup."""
    return bool(_HTML_BODY_RE.search(text[:2048]))


def parse_crawl_delay(robots_text: str) -> float | None:
    """The largest crawl-delay any agent block of robots.txt publishes
    (conservative: we honor the strictest ask rather than resolving
    user-agent precedence). None when absent or unparseable."""
    if is_html_body(robots_text):
        return None
    delays = []
    for raw in robots_text.splitlines():
        line = raw.split("#", 1)[0].strip()
        m = re.match(r"crawl-delay\s*:\s*(\d+(?:\.\d+)?)\s*$", line, re.IGNORECASE)
        if m:
            delays.append(float(m.group(1)))
    return max(delays) if delays else None


@dataclass(frozen=True)
class RobotsPolicy:
    """What one Portal's robots.txt asks of US: the published crawl delay and
    the path rules of the group that binds us. An unreachable or absent
    robots.txt is NO published ask, never a block: it yields an empty policy
    and the configured floor applies alone."""

    crawl_delay: float | None = None
    disallow: tuple[str, ...] = ()
    allow: tuple[str, ...] = ()

    def allows(self, url: str) -> bool:
        """RFC 9309 longest-match: the most specific rule wins, and a tie goes
        to Allow. An empty Disallow value is the documented escape hatch that
        disallows nothing, so it never appears in `disallow`."""
        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        best_block = max((len(r) for r in self.disallow if path.startswith(r)), default=-1)
        best_allow = max((len(r) for r in self.allow if path.startswith(r)), default=-1)
        return best_allow >= best_block


def parse_robots(robots_text: str, agent_token: str = USER_AGENT_TOKEN) -> RobotsPolicy:
    """Read robots.txt for OUR agent. A group naming us binds us; otherwise the
    `*` group does. The crawl delay stays the conservative reading of
    parse_crawl_delay (the strictest ask any group publishes), because honoring
    a stricter delay than asked can never be impolite.

    A body that is a web page rather than a robots file (the soft-404 Portals
    answer with their homepage) publishes NO rules: the empty policy, and the
    configured floor alone. Parsing markup for `Disallow:` lines would read
    permissions and refusals out of a page that states neither."""
    if is_html_body(robots_text):
        return RobotsPolicy()
    groups: list[tuple[set[str], list[str], list[str]]] = []
    agents: set[str] = set()
    rules: tuple[list[str], list[str]] | None = None
    for raw in robots_text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            if rules is not None:  # a new group starts after a rule block
                groups.append((agents, *rules))
                agents, rules = set(), None
            agents.add(value.lower())
        elif field in ("disallow", "allow"):
            if rules is None:
                rules = ([], [])
            if value:  # an empty Disallow value disallows nothing
                rules[0 if field == "disallow" else 1].append(value)
    if rules is not None:
        groups.append((agents, *rules))

    token = agent_token.lower()
    mine = [g for g in groups if any(token == a or a.startswith(f"{token}/") for a in g[0])]
    if not mine:
        mine = [g for g in groups if "*" in g[0]]
    disallow = tuple(r for g in mine for r in g[1])
    allow = tuple(r for g in mine for r in g[2])
    return RobotsPolicy(
        crawl_delay=parse_crawl_delay(robots_text), disallow=disallow, allow=allow
    )


# RFC 9309 section 2.3.1.4 has two halves. A robots.txt that answers 5xx is
# "unavailable" and asks for complete disallow; one that has STAYED that way
# for a reasonable period (the RFC names 30 days) may be treated as publishing
# no rules. This is that period, counted from the date recorded on the Portal
# in config/portals.yaml, so the second half is data a person wrote down after
# checking rather than a judgement the code makes on its own.
ROBOTS_UNREACHABLE_GRACE = timedelta(days=ROBOTS_UNREACHABLE_GRACE_DAYS)


class RobotsUnavailableError(RuntimeError):
    """The Portal answered its robots.txt with a server error (5xx), and the
    30-day rule has not lifted.

    RFC 9309 section 2.3.1.4 calls that status "unavailable" and asks a crawler
    to assume COMPLETE DISALLOW, which is a different thing from the 4xx case:
    a 404 says there are no rules, a 503 says there may well be rules and the
    Portal cannot show them right now. Guessing in that gap is exactly the kind
    of convenient assumption this project does not make, so the caller stops."""

    def __init__(
        self,
        host: str,
        status: int,
        *,
        since: date | None = None,
        lifts_on: date | None = None,
        scheme: str = "https",
    ):
        self.host = host
        self.status = status
        self.since = since
        self.lifts_on = lifts_on
        opening = (
            f"{scheme}://{host}/robots.txt answered HTTP {status}, so the Portal's"
            " published rules cannot be read. RFC 9309 asks a crawler to assume"
            " complete disallow while robots.txt is unavailable, so nothing is"
            " requested from this Portal."
        )
        if since is not None and lifts_on is not None:
            middle = (
                f" It has been unreachable since {since.isoformat()}; if it is"
                f" still unreachable on {lifts_on.isoformat()}, 30 days later,"
                " the refusal lifts and this Portal is crawled as though it"
                " published no rules."
            )
        else:
            middle = (
                " No robots_unreachable_since date is recorded for this Portal"
                " in config/portals.yaml. Check the Portal by hand, record the"
                " first date you saw it fail, and the refusal lifts 30 days"
                " after that date."
            )
        super().__init__(
            opening + middle + " Until then, retry when the Portal recovers,"
            ' or add each Document by hand with "Add document" by uploading'
            " the file."
        )


@dataclass(frozen=True)
class RobotsReading:
    """What one read of a Portal's robots.txt produced: the policy that binds
    us, and the sentence to record when something other than the Portal's own
    published rules let a request past an unreadable robots.txt. A reading with
    a note is never silent: the note goes onto the record of whatever made the
    request, and where the Portal's configured policy is what let it past, the
    status the Portal answered with and that policy are carried beside it so a
    record can state both as facts rather than as prose."""

    policy: RobotsPolicy
    note: str | None = None
    unavailable_status: int | None = None
    unavailable_policy: str | None = None


def read_robots_policy(
    host: str,
    fetch: Callable[[str], FetchResult],
    *,
    unreachable_since: date | None = None,
    today: date | None = None,
    unavailable_policy: str = "refuse",
    scheme: str = "https",
) -> RobotsReading:
    """The ONE place the robots rules are decided, for every lane that makes a
    request: Discovery and the single-URL add lane both come through here, so
    neither can end up politer or ruder than the other.

    2xx: the rules, parsed. 4xx or an unreachable host: no rules published.
    5xx: refused, with two documented exceptions, each of which yields an empty
    policy and a note saying which one applied. The first is RFC 9309's own
    second half: this Portal carries a `robots_unreachable_since` date more
    than 30 days old. The second is `unavailable_policy='proceed'`, the
    operator's recorded decision for THIS Portal that an unavailable robots.txt
    is read as no rules published; the reading then also carries the status and
    the policy, because a decision that is not on the record is not a
    disclosure. Neither exception touches a Portal that CAN serve its rules,
    and neither lowers the spacing floor.

    `scheme` is https everywhere except a host its Portal lists under
    `http_hosts`, whose robots.txt is read over http like its Documents."""
    try:
        text = _robots_text(host, fetch, scheme)
    except RobotsUnavailableError as exc:
        if unavailable_policy == "proceed":
            return RobotsReading(
                RobotsPolicy(),
                note=(
                    f"unavailable (HTTP {exc.status}), and this Portal is"
                    " configured robots_unavailable_policy: proceed, so it is"
                    " read as publishing no rules and the Portal's own spacing"
                    " floor applies"
                ),
                unavailable_status=exc.status,
                unavailable_policy=unavailable_policy,
            )
        if unreachable_since is None:
            raise
        today = today or date.today()
        if today - unreachable_since > ROBOTS_UNREACHABLE_GRACE:
            return RobotsReading(
                RobotsPolicy(),
                note=(
                    f"unreachable since {unreachable_since.isoformat()}, treated"
                    " as no restrictions per RFC 9309 after 30 days"
                ),
            )
        raise RobotsUnavailableError(
            host,
            exc.status,
            since=unreachable_since,
            lifts_on=unreachable_since + ROBOTS_UNREACHABLE_GRACE + timedelta(days=1),
            scheme=scheme,
        ) from exc
    return RobotsReading(RobotsPolicy() if text is None else parse_robots(text))


def robots_scheme(url: str, http_hosts) -> str:
    """The scheme to read a URL's robots.txt over: http for a plain-http URL
    on a host its Portal lists under `http_hosts`, https for everything else."""
    parts = urlsplit(url)
    if parts.scheme == "http" and (parts.hostname or "").lower() in set(http_hosts or ()):
        return "http"
    return "https"


def _robots_text(
    host: str, fetch: Callable[[str], FetchResult], scheme: str = "https"
) -> str | None:
    """The Portal's robots.txt body, or None when it published no rules.

    The three answers RFC 9309 distinguishes, and what each means here:
    - 2xx: the rules, parsed and obeyed.
    - 4xx (404 included): NO rules published. The configured floor applies
      alone; absence of an ask is not an ask to hurry.
    - 5xx: unavailable. RobotsUnavailableError, because the rules may exist and
      we cannot see them.
    A transport failure is treated as 4xx: we never reached the Portal at all,
    which is our problem rather than a statement by it, and robots
    unavailability of that kind must not turn every network blip into a refusal
    to fetch a Document a reviewer named.
    """
    try:
        fr = fetch(f"{scheme}://{host}/robots.txt")
    except Exception:  # noqa: BLE001 - a dead connection is not a published rule
        return None
    if fr.http_status >= 500:
        raise RobotsUnavailableError(host, fr.http_status, scheme=scheme)
    if fr.http_status >= 400:
        return None
    return fr.content.decode("utf-8", errors="replace")


def fetch_robots_policy(host: str, fetch: Callable[[str], FetchResult]) -> RobotsPolicy:
    """Read https://{host}/robots.txt through the SAME fetch strategy the
    Discovery uses (a plain fetch gets a CloudFront 403 on SG: exactly the
    blind spot that once recorded "publishes none" for a host that publishes
    crawl-delay 6).

    A 4xx or an unreachable host is an empty policy: absence of a published
    ask, not permission to ignore one that exists. A 5xx RAISES
    RobotsUnavailableError.

    This is the policy-only shortcut for callers that have no Portal in hand;
    read_robots_policy is the full reading, and the only one that can apply the
    30-day rule, because that rule lives on the Portal."""
    return read_robots_policy(host, fetch).policy


def published_crawl_delay(host: str, fetch: Callable[[str], FetchResult]) -> float | None:
    """Fetch https://{host}/robots.txt through the SAME per-economy fetch
    strategy the crawl uses (a plain fetch gets a CloudFront 403 on SG:
    exactly the blind spot that once recorded "publishes none" for a host
    that publishes crawl-delay 6). Any failure returns None and the config
    floor applies alone; robots can raise our politeness, never lower it.

    A 5xx raises RobotsUnavailableError here too: a crawl that cannot read the
    rules does not get to pick its own spacing instead."""
    text = _robots_text(host, fetch)
    return None if text is None else parse_crawl_delay(text)


def robots_aware_limiter(
    economy: str,
    seeds: CrawlSeedsEconomy,
    fetch: Callable[[str], FetchResult],
    storage: Storage,
) -> RateLimiter:
    """The published crawl-delay is enforced at RUN TIME: the effective
    interval is max(config floor, robots ask), so a portal raising its ask
    can never be undercut by a stale config note. The decision is written to
    audit_log.

    This reads robots.txt through read_robots_policy, the same door Discovery
    and the single-URL add lane use, so the bare crawl path obeys the same 5xx
    rule, the same 30-day rule and the same per-Portal unavailable-robots
    policy rather than a cruder copy of them: an unavailable robots.txt raises
    here too, and a note (the 30-day rule having lifted, or the Portal's policy
    having let it past) is written to audit_log where the decision lives."""
    from regcompass.config import CONFIG_DIR, load_portals

    host = robots_host(economy)
    portal = load_portals(CONFIG_DIR).get(economy)
    reading = (
        read_robots_policy(
            host,
            fetch,
            unreachable_since=portal.robots_unreachable_since if portal else None,
            unavailable_policy=(
                portal.robots_unavailable_policy if portal else "refuse"
            ),
        )
        if host
        else RobotsReading(RobotsPolicy())
    )
    published = reading.policy.crawl_delay
    interval = max(seeds.rate_limit_seconds, published or 0.0)
    with log_stage(
        storage, stage="m10_crawl", method=f"{economy}:robots",
        input_data=f"https://{host}/robots.txt" if host else None,
    ) as rec:
        rec.decision = (
            f"published={published} floor={seeds.rate_limit_seconds}"
            f" -> interval={interval}s"
            + (f"; robots {reading.note}" if reading.note else "")
        )
    return RateLimiter(interval)


def _transport_retrying(attempts: int = _TRANSPORT_ATTEMPTS, wait_max: float = 10.0):
    """Tenacity decorator for TRANSPORT errors only (never HTTP statuses):
    a 404 is an answer, a connection reset is not."""
    return retry(
        reraise=True,
        stop=stop_after_attempt(attempts),
        wait=wait_exponential(multiplier=0.5, max=wait_max),
        retry=retry_if_exception_type(httpx.TransportError),
    )


@contextmanager
def one_connection(
    *,
    user_agent: str = IDENTIFIED_USER_AGENT,
    timeout: float = 120.0,
    transport: httpx.BaseTransport | None = None,
):
    """ONE connection for one Discovery: a single pooled client capped at one
    connection, so a Portal sees one polite conversation rather than a burst of
    sockets. Discovery opens it once and every request of that Discovery goes
    through it."""
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": user_agent},
        transport=transport,
        event_hooks=_GUARD_HOOKS,
        limits=httpx.Limits(
            max_connections=CONNECTIONS_PER_HOST,
            max_keepalive_connections=CONNECTIONS_PER_HOST,
        ),
    ) as client:
        yield client


def fetch_httpx(
    url: str,
    *,
    timeout: float = 120.0,
    transport: httpx.BaseTransport | None = None,
    wait_max: float = 10.0,
    client: httpx.Client | None = None,
    attempts: int = _TRANSPORT_ATTEMPTS,
) -> FetchResult:
    """Plain httpx GET under the identified user agent (AU documents, MY PDFs,
    discovery JSON, and the first rung of the SG ladder). `client` reuses one
    Discovery's single connection instead of opening a fresh one per request."""

    @_transport_retrying(attempts=attempts, wait_max=wait_max)
    def _get() -> httpx.Response:
        if client is not None:
            return client.get(url)
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": IDENTIFIED_USER_AGENT},
            transport=transport,
            event_hooks=_GUARD_HOOKS,
        ) as c:
            return c.get(url)

    r = _get()
    return FetchResult(
        url=url,
        final_url=str(r.url),
        http_status=r.status_code,
        content=r.content,
        content_type=r.headers.get("content-type"),
        method="httpx",
    )


def fetch_curl_cffi(
    url: str,
    *,
    timeout: float = 120.0,
    before_request: Callable[[str], None] | None = None,
) -> FetchResult:
    """SG ladder rung 1: browser TLS/JA3 impersonation, no browser process.
    The most byte-stable option (no DOM, no re-flow). curl_cffi's own retry
    stays at its default 0; transport retries are NOT layered here because a
    WAF block manifests as a status, which the ladder handles."""
    from curl_cffi import requests as cffi_requests

    # Redirects followed by hand, so a never-requested host is refused before
    # it is asked, on this rung as on every other.
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        refuse_never_requested(current)
        if before_request is not None:
            # A bounded Discovery's deadline, checked before every hop.
            before_request(current)
        r = cffi_requests.get(
            current, impersonate="chrome", timeout=timeout, allow_redirects=False
        )
        location = r.headers.get("location")
        if 300 <= r.status_code < 400 and location:
            current = urljoin(current, location)
            continue
        break
    else:
        raise FetchFailedError(f"more than {MAX_REDIRECTS} redirects from {url}")
    return FetchResult(
        url=url,
        final_url=str(r.url),
        http_status=r.status_code,
        content=r.content,
        content_type=r.headers.get("content-type"),
        method="curl_cffi",
    )


def _route_guard(route) -> None:
    """Playwright route handler: every request the page makes is fetched here
    with redirects NOT followed, so a redirect's target is seen before the
    browser asks for it. A never-requested host, asked for directly or named by
    a redirect, is aborted unsent."""
    url = route.request.url
    if is_never_requested(_host(url)):
        route.abort()
        return
    response = route.fetch(max_redirects=0)
    location = response.headers.get("location")
    if 300 <= response.status < 400 and location:
        if is_never_requested(_host(urljoin(url, location))):
            route.abort()
            return
    route.fulfill(response=response)


def fetch_playwright(
    url: str,
    *,
    timeout_ms: int = 90_000,
    before_request: Callable[[str], None] | None = None,
) -> FetchResult:
    """SG ladder rung 2 / AU fallback: a real headless browser's TLS + headers.
    response.body() is the raw main-document response bytes as the server sent
    them, NOT the rendered DOM, so the byte-for-byte guarantee holds."""
    from playwright.sync_api import sync_playwright

    if before_request is not None:
        # Checked before a browser is launched at all.
        before_request(url)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=BROWSER_USER_AGENT)
            # Every request the page makes, redirects included, is checked
            # before it leaves: a never-requested host is aborted unsent.
            page.route("**/*", _route_guard)
            refuse_never_requested(url)
            resp = page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            if resp is None:
                raise RuntimeError(f"no response object for {url}")
            body = resp.body()
            return FetchResult(
                url=url,
                final_url=resp.url,
                http_status=resp.status,
                content=body,
                content_type=resp.headers.get("content-type"),
                method="playwright",
            )
        finally:
            browser.close()


# The SG escalation ladder, politest rung first. Rung 1 asks as OURSELVES under
# the identified user agent; the browser-impersonating rungs exist only for the
# CloudFront refusal that follows, and a Discovery that reaches them records
# `escalated_to_impersonation` in its report. Rung 4 (Patchright headful +
# xvfb/real Chrome) is documented and wired only if these three all die.
SG_LADDER: tuple[tuple[str, Callable[[str], FetchResult]], ...] = (
    ("httpx", fetch_httpx),
    ("curl_cffi", fetch_curl_cffi),
    ("playwright", fetch_playwright),
)


def fetch_with_ladder(
    url: str,
    rungs: tuple[tuple[str, Callable[[str], FetchResult]], ...] = SG_LADDER,
    *,
    stop_on_timeout: bool = False,
) -> FetchResult:
    """Try each rung in order; escalate on WAF-block statuses or a dead rung.
    A non-block HTTP failure (404, 500) is an ANSWER: returned as-is for the
    caller to record, because it would be the same on every rung.

    A refusal of ours (a never-requested host, a redirect off the allowed
    hosts) is never escalated: no rung may ask what the first one refused.
    With `stop_on_timeout`, a rung that ran out of time ends the ladder too:
    a silent host does not answer a browser either."""
    errors: list[str] = []
    statuses: list[int | None] = []
    for name, fn in rungs:
        try:
            fr = fn(url)
        except (NeverRequestedHostError, RedirectOffHostError, NotAskedError):
            raise
        except Exception as exc:  # a dead rung must not kill the ladder
            if stop_on_timeout and _is_timeout(exc):
                raise
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            statuses.append(None)
            continue
        if fr.http_status in _BLOCK_STATUSES:
            errors.append(f"{name}: HTTP {fr.http_status} (blocked)")
            statuses.append(fr.http_status)
            continue
        return fr
    raise LadderExhaustedError(
        f"all rungs blocked for {url}: {'; '.join(errors)}."
        " Next step: Patchright headful (documented rung 3).",
        statuses=tuple(statuses),
    )


# ---------------------------------------------------------------------------
# Discovery: seeds -> document URLs, per economy
# ---------------------------------------------------------------------------

JsonGetter = Callable[[str, dict | None], tuple[int, object]]


def _default_get_json(
    url: str,
    params: dict | None = None,
    *,
    client: httpx.Client | None = None,
    attempts: int = _TRANSPORT_ATTEMPTS,
) -> tuple[int, object]:
    @_transport_retrying(attempts=attempts)
    def _get() -> httpx.Response:
        if client is not None:
            return client.get(url, params=params)
        with httpx.Client(
            timeout=60.0,
            follow_redirects=True,
            headers={"User-Agent": IDENTIFIED_USER_AGENT},
            event_hooks=_GUARD_HOOKS,
        ) as c:
            return c.get(url, params=params)

    r = _get()
    try:
        return r.status_code, r.json()
    except json.JSONDecodeError as exc:
        raise DiscoveryError(f"non-JSON answer from {url}: HTTP {r.status_code}") from exc


# Discovery plans are registered by STRATEGY name, never by Economy, so a new
# Economy can reuse a plan with one line of configuration. The ladder plan is
# the one place where that is not quite enough: two Economies share its FETCH
# (ask as ourselves, escalate only after a refusal) and resolve their seeds
# differently, because a Singaporean act short code and an Indonesian download
# reference are not the same kind of thing. This registry is that fork, kept in
# one place and keyed by Economy, so the shared plan stays one entry in
# STRATEGIES. An Economy with no entry here gets Singapore's resolution, which
# is the plan's original behaviour.
LADDER_DISCOVERERS: dict[str, Callable[..., tuple[list[CrawlTarget], list[str]]]] = {}


def discover_sg(
    seeds: CrawlSeedsEconomy,
    economy: str = "SG",
    *,
    get_json: JsonGetter | None = None,  # unused: SSO codes resolve without a search
    limiter: RateLimiter | None = None,  # unused: no discovery request is made
    fetch: Callable[[str], FetchResult] | None = None,  # unused: nothing is fetched here
    robots: RobotsPolicy | None = None,  # unused: no discovery request is made
) -> tuple[list[CrawlTarget], list[str]]:
    """The ladder plan's seed resolution: Singapore's, or the one registered for
    this Economy in LADDER_DISCOVERERS.

    SSO act short codes resolve to whole-act PDF URLs; no network needed.

    ?ViewType=Pdf is REQUIRED: the plain /Act/{code} HTML page lazy-loads its
    provisions (front matter + TOC only for large acts - the PDPA page carries
    section 26's heading but not its body, verified 5 Jul 2026). The PDF is
    the official whole-act consolidation, born-digital. See docs/PORTALS.md."""
    plan = LADDER_DISCOVERERS.get(economy)
    if plan is not None:
        return plan(
            seeds, economy, get_json=get_json, limiter=limiter,
            fetch=fetch, robots=robots,
        )
    targets = [
        CrawlTarget(
            url=SG_ACT.format(code=code) + "?ViewType=Pdf",
            economy=economy,
            source_family=family,
            filename_hint=f"sso_agc_gov_sg_Act_{code}.pdf",
        )
        for family, fam in seeds.families.items()
        for code in fam.acts
    ]
    return targets, []


def _au_pick_title(query: str, hits: list[dict]) -> dict | None:
    """Exact register-name match only (casefold): seeds carry full act names,
    and a fuzzy pick could silently crawl the wrong statute."""
    for hit in hits:
        if hit.get("name", "").casefold() == query.casefold():
            return hit
    return None


def discover_au(
    seeds: CrawlSeedsEconomy,
    economy: str = "AU",
    *,
    get_json: JsonGetter = _default_get_json,
    limiter: RateLimiter | None = None,
    fetch: Callable[[str], FetchResult] | None = None,  # unused: the search answers JSON
    robots: RobotsPolicy | None = None,  # unused: the search endpoint is the API host
) -> tuple[list[CrawlTarget], list[str]]:
    """Register OData API: title search -> latest version -> authorised PDF
    volume URLs. The download URL shape is deterministic from version metadata
    (verified live 5 Jul 2026): /{titleId}/{start}/{retro}/text/original/pdf/{vol}."""
    targets: list[CrawlTarget] = []
    misses: list[str] = []
    for family, fam in seeds.families.items():
        for query in fam.queries:
            if limiter:
                limiter.wait(AU_API)
            odata_q = query.replace("'", "''")
            status, data = get_json(
                f"{AU_API}/titles",
                {
                    "$filter": f"contains(name,'{odata_q}')",
                    "$select": "id,name,collection,status",
                },
            )
            hits = data.get("value", []) if isinstance(data, dict) else []
            title = _au_pick_title(query, hits) if status == 200 else None
            if title is None:
                misses.append(f"AU/{family}: no exact register match for '{query}'")
                continue
            if limiter:
                limiter.wait(AU_API)
            status, version = get_json(
                f"{AU_API}/versions/find(titleId='{title['id']}',asAtSpecification='latest')",
                {"$expand": "documents"},
            )
            if status != 200 or not isinstance(version, dict):
                misses.append(f"AU/{family}: versions/find failed for '{query}' (HTTP {status})")
                continue
            start = str(version.get("start", ""))[:10]
            retro = str(version.get("retrospectiveStart") or version.get("start", ""))[:10]
            register_id = version.get("registerId", title["id"])
            pdf_docs = [d for d in version.get("documents", []) if d.get("format") == "Pdf"]
            if not pdf_docs:
                misses.append(f"AU/{family}: no PDF documents for '{query}'")
                continue
            # volumeNumber is 0 for single-volume acts and 1..n for multi-volume
            # compilations; the download URL takes the literal value either way
            # (verified live 5 Jul 2026: /pdf/0 and /pdf/1..3 both answer 200).
            for doc in sorted(pdf_docs, key=lambda d: d.get("volumeNumber", 0)):
                vol = doc.get("volumeNumber", 0)
                hint = f"{register_id}VOL{vol:02d}.pdf" if vol else f"{register_id}.pdf"
                targets.append(
                    CrawlTarget(
                        url=(
                            f"https://www.legislation.gov.au/{title['id']}/{start}/{retro}"
                            f"/text/original/pdf/{vol}"
                        ),
                        economy=economy,
                        source_family=family,
                        filename_hint=hint,
                    )
                )
    return targets, misses


_MY_HREF = re.compile(r'href="(https://lom\.agc\.gov\.my/ilims/[^"]+\.pdf)"', re.I)


def _my_pdf_url(doc: dict) -> tuple[str, str] | None:
    """Prefer the English PDF (BI), fall back to Malay (BM: the M2 OCR + M3
    gloss lane handles it). The portal's pre-built anchor (DOC2DOWNLOADBI/BM)
    is the primary source: for some acts the GENERATEPDF path field carries a
    doubled prefix that 500s (portal data bug, observed live 5 Jul 2026 on
    ACT 588), while the anchor href is correct. Returns (url, filename)."""
    for anchor_key, gen_key in (
        ("DOC2DOWNLOADBI", "DOC2DOWNLOADBI_GENERATEPDF"),
        ("DOC2DOWNLOADBM", "DOC2DOWNLOADBM_GENERATEPDF"),
    ):
        m = _MY_HREF.search(doc.get(anchor_key) or "")
        if m:
            url = m.group(1)
            name = unquote(url.rsplit("/", 1)[-1])
            return quote(url, safe="/:%"), name
        raw = doc.get(gen_key)
        if not raw:
            continue
        try:
            entries = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        if entries and entries[0].get("path") and entries[0].get("docName"):
            e = entries[0]
            return MY_ILIMS + e["path"] + quote(e["docName"]), e["docName"]
    return None


def _my_is_encrypted(data: object) -> bool:
    """The Portal's current search answer: an encrypted envelope
    ({"encrypted": true, "data": "<base64>"}) in place of the Solr result set."""
    return isinstance(data, dict) and bool(data.get("encrypted")) and "data" in data


def discover_my(
    seeds: CrawlSeedsEconomy,
    economy: str = "MY",
    *,
    get_json: JsonGetter = _default_get_json,
    limiter: RateLimiter | None = None,
    fetch: Callable[[str], FetchResult] | None = None,  # unused: the proxy answers JSON
    robots: RobotsPolicy | None = None,  # unused: the fetch loop gates the PDF URLs
) -> tuple[list[CrawlTarget], list[str]]:
    """The portal's fess search proxy returns Solr JSON; each doc carries
    {path, docName} for its direct PDF under /ilims/upload/portal/akta/."""
    targets: list[CrawlTarget] = []
    misses: list[str] = []
    for family, fam in seeds.families.items():
        for query in fam.queries:
            if limiter:
                limiter.wait(MY_FESS)
            status, data = get_json(
                MY_FESS,
                {
                    "q": f'"{query}"',
                    "fq": "",
                    "start": 0,
                    "rows": 20,
                    "sort": "publicationDate desc",
                    "lookup": "all",
                    # principal act categories PLUS amendment acts: the ESCAP
                    # database itself cites amendment acts as evidence (MY 7.4
                    # rests on the PDPA (Amendment) Act 2024, A1727)
                    "kategori": (
                        "iagcact,iagcact_repealed,iagcact_revised,"
                        "iagcact_translated,iagcact_updated,iagcact_amendment"
                    ),
                    "draw": 1,
                },
            )
            if _my_is_encrypted(data):
                # Verified live 16 Sep 2026 (one recorded request, fixture in
                # tests/fixtures/portals/my/): the proxy now answers
                # {"encrypted": true, "data": "<base64>"} and the Solr envelope
                # is no longer on the wire. Saying so is the whole repair we can
                # honestly make: the payload needs the key the Portal's own page
                # script holds. An empty result set here would blame the search
                # terms for a change in the Portal.
                misses.append(
                    f"MY/{family}: the Portal returned an ENCRYPTED search payload"
                    f" for '{query}' (HTTP {status}); its documents cannot be read"
                    " without the key the Portal's page script holds."
                    " See tests/fixtures/portals/my/README.md and docs/PORTALS.md."
                )
                continue
            docs = (
                data.get("response", {}).get("docs", []) if isinstance(data, dict) else []
            )
            if status != 200 or not docs:
                misses.append(f"MY/{family}: no portal hits for '{query}' (HTTP {status})")
                continue
            found = False
            for doc in docs:
                picked = _my_pdf_url(doc)
                if picked is None:
                    continue
                url, doc_name = picked
                targets.append(
                    CrawlTarget(
                        url=url,
                        economy=economy,
                        source_family=family,
                        filename_hint=doc_name,
                    )
                )
                found = True
            if not found:
                misses.append(f"MY/{family}: hits for '{query}' carry no PDF links")
    return targets, misses


_IN_LEADING_THE = re.compile(r"^the\s+", re.IGNORECASE)


def _in_title_key(title: str) -> str:
    """The comparable form of an India Code title. The Portal prints its own
    titles inconsistently ("The Digital Personal Data Protection Act, 2023."
    carries a full stop, "The Telecommunications Act, 2023." too, while others
    do not), so the trailing stop, the leading article and runs of whitespace
    are normalised away. Nothing else is: the match stays EXACT, because a
    fuzzy pick would quietly crawl the wrong statute."""
    key = " ".join(title.split()).strip().rstrip(".").casefold()
    return _IN_LEADING_THE.sub("", key)


# An RDTII citation of an Indian statute: an optional "Government of India,"
# lead, the short title, an optional act number and the year
# ("Government of India, Information Technology Act No.21 2000").
_IN_CITATION = re.compile(
    r"^(?:government of india,?\s+)?(?:the\s+)?"
    r"(?P<title>[A-Z](?:[^,()]|\([^()]*\))*?\b(?:Act|Code))"
    r"(?:\s*,?\s*No\.?\s*[\dIVXLC]+)?\s*,?\s+(?P<year>1[89]\d\d|20\d\d)\.?$",
    re.IGNORECASE,
)


def in_citation_title(name: str) -> str:
    """The India Code title form of an RDTII citation of an Act or Code
    ("Government of India, Information Technology Act No.21 2000" becomes
    "The Information Technology Act, 2000"), for the exact title search. A
    name of any other shape is returned as it is: the search stays exact."""
    cited = " ".join(name.split())
    match = _IN_CITATION.match(cited)
    if not match:
        return cited
    return f"The {match.group('title')}, {match.group('year')}"


def _in_original_pdfs(item: dict) -> list[tuple[str, str]]:
    """The (url, filename) of every PDF in this item's ORIGINAL bundle.

    DSpace keeps three renderings of one act: ORIGINAL (the official PDF as
    published), TEXT (an extracted text rendering) and THUMBNAIL. Only ORIGINAL
    is the Document; the text rendering is the Portal's extraction, not the
    source, and quoting it would put someone else's OCR behind our citations."""
    bundles = (
        item.get("_embedded", {})
        .get("bundles", {})
        .get("_embedded", {})
        .get("bundles", [])
    )
    found: list[tuple[str, str]] = []
    for bundle in bundles:
        if bundle.get("name") != "ORIGINAL":
            continue
        streams = (
            bundle.get("_embedded", {})
            .get("bitstreams", {})
            .get("_embedded", {})
            .get("bitstreams", [])
        )
        for stream in streams:
            name = stream.get("name") or ""
            href = stream.get("_links", {}).get("content", {}).get("href")
            if href and name.lower().endswith(".pdf"):
                found.append((href, name))
    return found


def discover_in(
    seeds: CrawlSeedsEconomy,
    economy: str = "IN",
    *,
    get_json: JsonGetter = _default_get_json,
    limiter: RateLimiter | None = None,
    fetch: Callable[[str], FetchResult] | None = None,  # unused: the API answers JSON
    robots: RobotsPolicy | None = None,  # unused: the fetch loop gates the PDF URLs
) -> tuple[list[CrawlTarget], list[str]]:
    """India Code (DSpace 7) item search -> the act's own ORIGINAL PDF.

    ONE request per seed: the search asks for `embed=bundles/bitstreams`, so
    the answer already carries the download URL and neither the item nor its
    bundles need a second call.

    The Portal indexes every SECTION of an act as its own item ("Application of
    Act.", "Right to nominate."), and those carry no PDF. Only an exact title
    match may become a Document, as on the Australian register, and a seed that
    matches nothing is recorded as a miss rather than resolved to the nearest
    hit. Where India Code holds several items under one exact title (the
    Information Technology Act, 2000 appears more than once), each is emitted:
    identical bytes collapse in the crawl loop's sha256 dedupe, and genuinely
    different texts are a fact about the Portal that a reviewer should see."""
    targets: list[CrawlTarget] = []
    misses: list[str] = []
    for family, fam in seeds.families.items():
        for query in fam.queries:
            if limiter:
                limiter.wait(IN_SEARCH)
            status, data = get_json(
                IN_SEARCH,
                {
                    "query": query,
                    "dsoType": "item",
                    "size": 20,
                    "embed": "bundles/bitstreams",
                },
            )
            objects = (
                data.get("_embedded", {})
                .get("searchResult", {})
                .get("_embedded", {})
                .get("objects", [])
                if isinstance(data, dict)
                else []
            )
            if status != 200 or not objects:
                misses.append(
                    f"IN/{family}: no India Code answer for '{query}' (HTTP {status})"
                )
                continue
            wanted = _in_title_key(query)
            items = [
                obj.get("_embedded", {}).get("indexableObject", {}) for obj in objects
            ]
            exact = [it for it in items if _in_title_key(it.get("name") or "") == wanted]
            if not exact:
                misses.append(
                    f"IN/{family}: no exact India Code title match for '{query}'"
                )
                continue
            found = False
            for item in exact:
                for url, name in _in_original_pdfs(item):
                    targets.append(
                        CrawlTarget(
                            url=url,
                            economy=economy,
                            source_family=family,
                            filename_hint=name,
                        )
                    )
                    found = True
            if not found:
                misses.append(
                    f"IN/{family}: the India Code item for '{query}' carries no PDF"
                    " in its ORIGINAL bundle"
                )
    return targets, misses


# ---------------------------------------------------------------------------
# Lao PDR: the Official Gazette's own legislation grid
# ---------------------------------------------------------------------------

LA_GAZETTE = "https://laoofficialgazette.gov.la"
LA_LISTING = f"{LA_GAZETTE}/index.php?r=site/index"
# The grid that lists the instruments in force. The page carries a SECOND,
# unfiltered table of recent publications whose rows look identical, so every
# read is scoped to this element and never to the page.
LA_GRID_ID = "homelegal-grid"
# The grid's own PDF columns, by the Lao words in their headers rather than by
# position: "PDF ອັງກິດ" (English) and "PDF ລາວ" (Lao). The Lao column is the
# official text and the only quote source; the English one is a rendering the
# Portal offers beside it.
LA_ENGLISH_HEADER = "ອັງກິດ"
LA_LAO_HEADER = "ລາວ"
LA_ENGLISH_NOTE = "the Portal also publishes an English rendering (not the quote source): "


class LaoGridError(DiscoveryError):
    """The Official Gazette's legislation grid was not on the page in the shape
    the adapter reads. Named rather than swallowed: an empty result set would
    blame the search terms for a change in the Portal (the Malaysian lesson)."""


@dataclass(frozen=True)
class LaoLegalRow:
    """One row of the Official Gazette's legislation grid: an instrument's Lao
    title and the file or files the Portal publishes it as."""

    title: str
    lao_pdf_url: str | None
    english_pdf_url: str | None


def la_listing_url(title_filter: str, page: int = 1) -> str:
    """The grid's own URL for one title filter and one page. The filter is the
    grid's `Document[title]` input, which matches anywhere in the title, so a
    source family is seeded by a Lao word ("ໂທລະຄົມ", telecommunications) and
    never by an Indicator question. Page 1 carries no page parameter, exactly
    as the Portal's own pager link does."""
    query = quote(f"Document[title]={title_filter}", safe="=&")
    paging = f"&Document_page={page}" if page > 1 else ""
    return f"{LA_LISTING}&{query}{paging}"


def _la_absolute(href: str) -> str:
    """One grid href as a whole URL. The Portal stores files under their own
    Lao names, spaces included, so the path is percent-encoded here and the
    already-encoded parts are left alone."""
    from html import unescape

    raw = unescape(href.strip())
    if raw.startswith("http"):
        return quote(raw, safe="/:%?&=")
    return quote(f"{LA_GAZETTE}{raw if raw.startswith('/') else '/' + raw}", safe="/:%?&=")


def _la_cell_pdf(cell) -> str | None:
    """The PDF URL a grid cell offers, or None where the cell is empty."""
    for anchor in cell.find_all("a"):
        href = anchor.get("href") or ""
        if href.lower().endswith(".pdf"):
            return _la_absolute(href)
    return None


def parse_la_listing(html: str) -> tuple[list[LaoLegalRow], int, int]:
    """The legislation grid of one listing page: its rows, how many results the
    page shows, and how many the filter matched in total.

    The last two come from the grid's own summary line ("ສະແດງ 1-10 ຂອງ 17"),
    which is what tells the walk whether another page exists without trusting
    a pager that is absent whenever the results fit one page."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    grid = soup.find(id=LA_GRID_ID)
    if grid is None:
        raise LaoGridError(
            f"the Official Gazette page carries no '{LA_GRID_ID}' legislation grid;"
            " the Portal's page shape has changed"
        )
    table = grid.find("table")
    if table is None:
        raise LaoGridError("the legislation grid carries no results table")
    headers = [th.get_text(" ", strip=True) for th in table.find_all("th")]

    def _column(word: str) -> int | None:
        """The index of the PDF column whose header carries this word. Both
        words are checked, because ລາວ (Lao) alone is a word the Portal uses
        elsewhere and only "PDF ລາວ" is a file column."""
        return next(
            (i for i, h in enumerate(headers) if "PDF" in h.upper() and word in h), None
        )

    english_col = _column(LA_ENGLISH_HEADER)
    lao_col = _column(LA_LAO_HEADER)
    if lao_col is None:
        raise LaoGridError(
            "the legislation grid has no Lao PDF column"
            f" (headers: {headers}); the Portal's columns have changed"
        )

    rows: list[LaoLegalRow] = []
    body = table.find("tbody")
    for tr in (body.find_all("tr", recursive=False) if body else []):
        cells = tr.find_all("td", recursive=False)
        if len(cells) <= lao_col:
            continue  # the grid's own "no results" row
        rows.append(
            LaoLegalRow(
                title=cells[0].get_text(" ", strip=True),
                lao_pdf_url=_la_cell_pdf(cells[lao_col]),
                english_pdf_url=(
                    _la_cell_pdf(cells[english_col])
                    if english_col is not None and len(cells) > english_col
                    else None
                ),
            )
        )

    summary = grid.find("div", class_="summary")
    numbers = [int(n) for n in re.findall(r"\d+", summary.get_text() if summary else "")]
    shown, total = (numbers[-2], numbers[-1]) if len(numbers) >= 3 else (len(rows), len(rows))
    return rows, shown, total


def discover_la(
    seeds: CrawlSeedsEconomy,
    economy: str = "LA",
    *,
    get_json: JsonGetter | None = None,  # unused: the Portal answers in HTML
    limiter: RateLimiter | None = None,
    fetch: Callable[[str], FetchResult] | None = None,
    robots: RobotsPolicy | None = None,
) -> tuple[list[CrawlTarget], list[str]]:
    """The Official Gazette's legislation grid, filtered by one Lao word per
    source family and walked to the bound the seeds set.

    Each row of the grid carries the official Lao PDF and, for a few
    instruments, an English rendering beside it. The Lao file is the Document;
    the English URL travels on the target's notes and is stored on the Corpus
    row, which is where a submission's Notes can cite it as a secondary
    reference. It is never the text a quote is taken from.
    Verified against the Portal on 16 Sep 2026
    (tests/fixtures/portals/la/README.md); docs/PORTALS.md carries the quirks."""
    fetch = fetch or fetch_httpx
    targets: list[CrawlTarget] = []
    misses: list[str] = []
    seen: set[str] = set()
    for family, fam in seeds.families.items():
        for query in fam.queries:
            found = 0
            for page in range(1, seeds.max_pages + 1):
                url = la_listing_url(query, page)
                if robots is not None and not robots.allows(url):
                    misses.append(
                        f"{economy}/{family}: robots.txt disallows the listing"
                        f" {url}; not requested"
                    )
                    break
                if limiter:
                    limiter.wait(url)
                result = fetch(url)
                if result.http_status >= 400:
                    misses.append(
                        f"{economy}/{family}: the Portal answered HTTP"
                        f" {result.http_status} for '{query}' (page {page})"
                    )
                    break
                try:
                    rows, shown, total = parse_la_listing(
                        result.content.decode("utf-8", errors="replace")
                    )
                except LaoGridError as exc:
                    misses.append(f"{economy}/{family}: {exc}")
                    break
                for row in rows:
                    if row.lao_pdf_url is None:
                        if row.english_pdf_url is not None:
                            misses.append(
                                f"{economy}/{family}: '{row.title}' offers only an"
                                " English rendering, which is never the quote source;"
                                " skipped"
                            )
                        continue
                    if row.lao_pdf_url in seen:
                        # One instrument can match two source families. It is a
                        # Document of this family too, so it counts as found and
                        # is registered once.
                        found += 1
                        continue
                    seen.add(row.lao_pdf_url)
                    targets.append(
                        CrawlTarget(
                            url=row.lao_pdf_url,
                            economy=economy,
                            source_family=family,
                            filename_hint=unquote(row.lao_pdf_url.rsplit("/", 1)[-1]),
                            notes=(
                                LA_ENGLISH_NOTE + row.english_pdf_url
                                if row.english_pdf_url
                                else None
                            ),
                        )
                    )
                    found += 1
                if not rows or shown >= total:
                    break
            if not found:
                misses.append(f"{economy}/{family}: no Documents for '{query}'")
    return targets, misses


# The `httpx` plan covers every Portal that answers our identified agent over
# plain HTTP with its own search or listing. The shape of that answer is the
# Portal's business, so the plan dispatches to one adapter per Economy rather
# than pretending Malaysia's Solr proxy and the Lao grid are one parser.
PORTAL_SEARCH_DISCOVERERS: dict[str, Callable[..., tuple[list[CrawlTarget], list[str]]]] = {
    "MY": discover_my,
    "LA": discover_la,
}


def discover_portal_search(
    seeds: CrawlSeedsEconomy,
    economy: str,
    *,
    get_json: JsonGetter = _default_get_json,
    limiter: RateLimiter | None = None,
    fetch: Callable[[str], FetchResult] | None = None,
    robots: RobotsPolicy | None = None,
) -> tuple[list[CrawlTarget], list[str]]:
    """This Economy's adapter for the `httpx` plan."""
    adapter = PORTAL_SEARCH_DISCOVERERS.get(economy)
    if adapter is None:
        raise DiscoveryError(
            f"{economy} is configured for the 'httpx' Discovery plan but has no"
            f" adapter; the plan covers {sorted(PORTAL_SEARCH_DISCOVERERS)}"
        )
    return adapter(seeds, economy, get_json=get_json, limiter=limiter, fetch=fetch, robots=robots)


def discover_id(
    seeds: CrawlSeedsEconomy,
    economy: str = "ID",
    *,
    get_json: JsonGetter | None = None,  # unused: references resolve without a search
    limiter: RateLimiter | None = None,  # unused: no discovery request is made
    fetch: Callable[[str], FetchResult] | None = None,  # unused: nothing is fetched here
    robots: RobotsPolicy | None = None,  # unused: no discovery request is made
) -> tuple[list[CrawlTarget], list[str]]:
    """Indonesian download references resolve to statute PDF URLs; no network
    needed, exactly as Singapore's act short codes do.

    A seed is the tail of the Portal's own download path, `{file id}/{file
    name}.pdf`, written out in full because the Portal does not let it be
    computed: the file id differs from the id in the /Details/ page URL
    (Law 27/2022 is detail 229798 and download 224884, verified 16 Sep 2026).
    Each reference was read off a search listing that is committed under
    tests/fixtures/portals/id/, and a test asserts that every seed still
    appears in one of them, so a seed cannot be invented.

    The file name is percent-encoded and nothing else: the Portal serves names
    with double spaces (`UU Nomor  19 Tahun 2016.pdf`) and normalising one
    would 404.

    Discovery asks this Portal nothing. The only requests it makes are
    robots.txt and the Documents themselves, through the ladder, because the
    Portal's published rules permit us while its Cloudflare edge refuses the
    identified user agent (docs/PORTALS.md)."""
    targets: list[CrawlTarget] = []
    misses: list[str] = []
    for family, fam in seeds.families.items():
        for reference in fam.acts:
            name = reference.rsplit("/", 1)[-1]
            if not name:
                misses.append(
                    f"{economy}/{family}: '{reference}' names no file; a seed is"
                    " '{file id}/{file name}.pdf' as the Portal spells it"
                )
                continue
            targets.append(
                CrawlTarget(
                    url=ID_DOWNLOAD.format(ref=quote(reference, safe="/")),
                    economy=economy,
                    source_family=family,
                    filename_hint=name,
                )
            )
    return targets, misses


LADDER_DISCOVERERS["ID"] = discover_id


@dataclass(frozen=True)
class Strategy:
    """One Discovery plan: how targets are found and how bytes are fetched.
    `discover` is None for the manual-acquisition plan, which fetches nothing.

    `discovery_needs_network` says whether resolving seeds to Document URLs
    asks the Portal anything. Singapore's act short codes resolve arithmetically
    and ask nothing; the Australian and Malaysian plans query the Portal's own
    search API. Discovery reads robots.txt before the first request either way,
    and the flag is what lets it know whether a Discovery with nothing left to
    fetch can finish without making any request at all."""

    discover: Callable[..., tuple[list[CrawlTarget], list[str]]] | None
    fetch: Callable[[str], FetchResult] | None
    description: str
    discovery_needs_network: bool = True


# The Discovery strategy registry, keyed by the `strategy` name in
# config/portals.yaml, NEVER by the Economy code. That is what lets a new
# Economy reuse an existing plan with one line of configuration. The names here
# must be exactly contracts.STRATEGY_NAMES (a test asserts it), because that is
# the vocabulary PortalConfig validates against on the base tier.
STRATEGIES: dict[str, Strategy] = {
    "curl_cffi_ladder": Strategy(
        discover=discover_sg,
        fetch=fetch_with_ladder,
        description="SSO act short codes; identified fetch, impersonation ladder on refusal",
        discovery_needs_network=False,  # short codes resolve without asking the Portal
    ),
    "playwright": Strategy(
        discover=discover_au,
        fetch=fetch_httpx,
        description="register OData API search; deterministic PDF volume URLs",
    ),
    "httpx": Strategy(
        discover=discover_portal_search,
        fetch=fetch_httpx,
        description="portal search or listing; direct PDF fetches",
    ),
    "dspace_rest": Strategy(
        discover=discover_in,
        fetch=fetch_httpx,
        description=(
            "DSpace public read-only REST API; exact-title item search,"
            " direct ORIGINAL-bundle PDF URLs"
        ),
    ),
    MANUAL_STRATEGY: Strategy(
        discover=None,
        fetch=None,
        description="no automated collection; Documents are added by hand",
        discovery_needs_network=False,
    ),
}


def strategy_for_economy(economy: str, config_dir=None) -> Strategy:
    """This Economy's Discovery plan, read off its Portal configuration."""
    from regcompass.config import CONFIG_DIR, load_portals

    portals = load_portals(config_dir or CONFIG_DIR)
    if economy not in portals:
        raise ValueError(
            f"unknown economy '{economy}': configured economies are {sorted(portals)}"
        )
    return STRATEGIES[portals[economy].strategy]


def discover_targets(
    economy: str,
    seeds: CrawlSeedsEconomy,
    *,
    get_json: JsonGetter = _default_get_json,
    limiter: RateLimiter | None = None,
    fetch: Callable[[str], FetchResult] | None = None,
    robots: RobotsPolicy | None = None,
    config_dir=None,
) -> tuple[list[CrawlTarget], list[str]]:
    """Seed targets for one Economy through its configured strategy. A
    manual-acquisition Economy discovers nothing and says so; it is not an
    error, it is the configured answer.

    `fetch` and `robots` are for the plans whose targets come out of the
    Portal's own HTML listing rather than a JSON search: the listing pages go
    through the SAME fetch the Documents do, under the same published rules."""
    strategy = strategy_for_economy(economy, config_dir)
    if strategy.discover is None:
        return [], [
            f"{economy}: {strategy.description}; Discovery found no targets by design"
        ]
    return strategy.discover(
        seeds, economy, get_json=get_json, limiter=limiter, fetch=fetch, robots=robots
    )


# ---------------------------------------------------------------------------
# Crawl driver: manifest-driven fetch with resume + dedupe
# ---------------------------------------------------------------------------


def _safe_name(hint: str | None, url: str) -> str:
    base = hint or (urlsplit(url).path.rsplit("/", 1)[-1] or "document")
    base = re.sub(r"[^A-Za-z0-9._()-]+", "_", base).strip("._") or "document"
    return base[:120]


def session_fetch(strategy: Strategy, client: httpx.Client) -> Callable[[str], FetchResult]:
    """This Strategy's fetch, bound to ONE Discovery's single connection. The
    httpx rungs take the shared client; the impersonating rungs cannot (they
    own their own transport), which is one more reason they are an escalation
    and not the default."""
    if strategy.fetch is None:
        raise ValueError(f"{strategy.description}; there is nothing to fetch")
    if strategy.fetch is fetch_httpx:
        return partial(fetch_httpx, client=client)
    if strategy.fetch is fetch_with_ladder:
        rungs = tuple(
            (name, partial(fn, client=client)) if fn is fetch_httpx else (name, fn)
            for name, fn in SG_LADDER
        )
        return partial(fetch_with_ladder, rungs=rungs)
    return strategy.fetch


def bounded_session_fetch(
    strategy: Strategy,
    client: httpx.Client,
    *,
    timeout: float,
    gate: Callable[[str], None] | None = None,
) -> Callable[[str], FetchResult]:
    """This Strategy's fetch for a Discovery with a deadline: one attempt per
    rung, a short timeout on each, and a ladder that stops at a timeout
    instead of climbing to a browser for a host that is simply silent."""
    if strategy.fetch is None:
        raise ValueError(f"{strategy.description}; there is nothing to fetch")
    one_try = partial(fetch_httpx, client=client, attempts=1)
    if strategy.fetch is fetch_httpx:
        return one_try
    if strategy.fetch is fetch_with_ladder:
        rungs = (
            ("httpx", one_try),
            ("curl_cffi", partial(fetch_curl_cffi, timeout=timeout, before_request=gate)),
            (
                "playwright",
                partial(fetch_playwright, timeout_ms=int(timeout * 1000), before_request=gate),
            ),
        )
        return partial(fetch_with_ladder, rungs=rungs, stop_on_timeout=True)
    return strategy.fetch


def session_get_json(client: httpx.Client) -> JsonGetter:
    """The discovery JSON getter on the same single connection."""
    return partial(_default_get_json, client=client)


def strategy_for(economy: str, config_dir=None) -> Callable[[str], FetchResult]:
    """The fetch function of this Economy's configured Discovery strategy."""
    strategy = strategy_for_economy(economy, config_dir)
    if strategy.fetch is None:
        raise ValueError(
            f"{economy}: {strategy.description}; there is nothing to fetch."
            " Add the Document by hand instead."
        )
    return strategy.fetch


def fetch_one(
    url: str,
    economy: str,
    *,
    fetch: Callable[[str], FetchResult] | None = None,
    limiter: RateLimiter | None = None,
    robots: RobotsPolicy | None = None,
    config_dir=None,
    robots_reading: Callable[["RobotsReading"], None] = lambda reading: None,
    allowed_hosts: set[str] | frozenset[str] | list[str] | None = None,
) -> FetchResult:
    """ONE polite fetch of ONE URL a reviewer supplied ("Add document" by
    Source URL).

    It makes the same three promises the crawl loop makes, in the same order,
    because a single request is not a licence to drop them: the Portal's
    published robots.txt is read and obeyed, the politeness floor is waited out
    (the configured floor, raised by any published crawl-delay), and the
    request goes out under the identified user agent.

    The fetch defaults to this Economy's configured Discovery strategy, and to
    plain identified httpx where the Economy has no strategy yet: a Document
    the operator names is one URL, not a crawl, so it needs no escalation
    ladder to find it. `fetch`, `limiter` and `robots` are injectable so tests
    drive the lane over recorded answers and make no live call.

    This lane reads robots.txt through the SAME read_robots_policy Discovery
    uses, so the two cannot end up with different manners, and `robots_reading`
    receives that reading whenever the Portal's robots.txt was unreadable and
    something other than its published rules let this request past: the 30-day
    rule, or the Portal's own configured policy. The caller records it."""
    # Redirects are followed by hand, each address checked before it is
    # asked: never a never-requested host, and with `allowed_hosts` given,
    # never a host outside them.
    if fetch is None:
        from regcompass.config import CONFIG_DIR as _DIR
        from regcompass.config import load_portals as _portals

        known = _portals(config_dir or _DIR).get(economy)
        fetch = hop_for(known.strategy if known is not None else "httpx", timeout=60.0)
    hop = fetch
    allowed = set(allowed_hosts) if allowed_hosts is not None else None

    def guarded(target: str) -> FetchResult:
        return follow_guarded(target, hop, allowed=allowed)

    fetch = guarded

    refuse_never_requested(url)
    host = urlsplit(url).netloc
    if robots is None:
        from regcompass.config import CONFIG_DIR as _CONFIG_DIR
        from regcompass.config import load_portals as _load_portals

        known = _load_portals(config_dir or _CONFIG_DIR).get(economy)
        reading = read_robots_policy(
            host,
            fetch,
            unreachable_since=(
                known.robots_unreachable_since if known is not None else None
            ),
            unavailable_policy=(
                known.robots_unavailable_policy if known is not None else "refuse"
            ),
            scheme=robots_scheme(url, known.http_hosts if known is not None else ()),
        )
        robots = reading.policy
        if reading.note:
            robots_reading(reading)
    if not robots.allows(url):
        raise RobotsDisallowedError(f"{ROBOTS_DISALLOWED_ERROR}: {url}")
    if limiter is None:
        from regcompass.config import CONFIG_DIR, load_portals

        portal = load_portals(config_dir or CONFIG_DIR).get(economy)
        floor = portal.min_interval_seconds if portal is not None else 1.0
        limiter = RateLimiter(max(floor, robots.crawl_delay or 0.0))
    limiter.wait(url)
    fr = fetch(url)
    if fr.http_status >= 400:
        raise FetchFailedError(f"HTTP {fr.http_status} from {url}")
    if not fr.content:
        raise FetchFailedError(f"{url} answered with an empty body")
    return fr


def _not_asked(exc: NotAskedError, rows, hook, progress) -> bool:
    """Tell the hook each row a Discovery chose not to ask for, left pending.
    True when the loop must stop (the deadline), False to go on."""
    code = "time_limit" if isinstance(exc, DeadlineReachedError) else "fetch_error"
    for row in rows:
        progress(f"M10 crawl | not asked ({exc}): {row['url']}")
        if code == "time_limit":
            hook.skipped(row["url"], "limit", str(exc))
        else:
            hook.skipped(row["url"], "fetch_error", skip_reason("fetch_error", str(exc)))
    return isinstance(exc, DeadlineReachedError)


def crawl_economy(
    economy: str,
    storage: Storage,
    data_dir: Path,
    seeds: CrawlSeedsEconomy,
    *,
    fetcher: Callable[[str], FetchResult] | None = None,
    get_json: JsonGetter = _default_get_json,
    limiter: RateLimiter | None = None,
    max_documents: int | None = None,
    targets: list[CrawlTarget] | None = None,
    robots: RobotsPolicy | None = None,
    progress: Callable[[str], None] = lambda s: None,
    hook: DiscoveryProgress | None = None,
    gate: Callable[[str], None] | None = None,
) -> CrawlReport:
    """Discover seed targets, then fetch every pending manifest row for this
    economy. Resume is free: fetched rows are skipped, failed rows stay failed
    (recorded, not retried forever). Exact bytes land in data/<economy>/raw/.

    `targets` supplies an already-resolved target list, which is how Discovery
    (discovery.discover_economy) hands over the targets it has already filtered
    against the Documents already in the Corpus; left None, this resolves the
    seeds itself as it always has.

    `robots` is the Portal's published policy, and it binds THE FETCH LOOP, not
    the target list: a pending row can outlive the call that created it (a
    max_documents bound, a Discovery that died, a Document registered by hand),
    and the refusal has to hold where the request is about to be made. A
    disallowed row is settled in the manifest and never requested. Left None
    (the bare `crawl` path) nothing is gated, exactly as before.

    progress is an optional live per-URL echo (fetched / duplicate / failed) for
    the CLI and web demo; the resumable audit trail is still audit_log.

    `hook` hears each Document fetched or skipped, with the reason in plain
    words (regcompass.discovery_progress); left None, nothing listens."""
    hook = hook if hook is not None else DiscoveryProgress()
    report = CrawlReport(economy=economy)
    fetch = fetcher or strategy_for(economy)
    limiter = limiter or robots_aware_limiter(economy, seeds, fetch, storage)

    if targets is None:
        targets, report.misses = discover_targets(
            economy, seeds, get_json=get_json, limiter=limiter, fetch=fetch, robots=robots
        )
    for t in targets:
        if storage.manifest_add_pending(
            t.url, t.economy, kind=t.kind,
            source_family=t.source_family, filename_hint=t.filename_hint,
            notes=t.notes,
        ):
            report.discovered += 1
    progress(
        f"M10 crawl | {economy}: discovered {report.discovered} seed target(s),"
        f" politeness floor {limiter.min_interval}s/host"
    )

    raw_dir = data_dir / economy / "raw"
    pending = storage.manifest_rows(economy=economy, status="pending")
    done = len(storage.manifest_rows(economy=economy, status="fetched"))
    report.skipped_resume = done

    for i, row in enumerate(pending):
        if max_documents is not None and report.fetched + report.deduplicated >= max_documents:
            # Left pending for the next call, and said so rather than dropped
            # from the picture in silence.
            for rest in pending[i:]:
                hook.skipped(rest["url"], "limit", skip_reason("limit"))
            break
        url = row["url"]
        if robots is not None and not robots.allows(url):
            # Settled without a request: counted once, here, where every route
            # to a fetch passes. The manifest keeps the evidence that we found
            # the URL and chose not to take it.
            storage.manifest_mark_failed(url, error=ROBOTS_DISALLOWED_ERROR)
            report.disallowed += 1
            report.misses.append(f"{economy}: robots.txt disallows {url}; skipped")
            progress(f"M10 crawl | robots.txt disallows, skipped: {url}")
            hook.skipped(url, "robots", skip_reason("robots"))
            continue
        try:
            # A bounded Discovery's own rules (its deadline, the hosts that
            # did not answer), checked before the politeness wait.
            if gate is not None:
                gate(url)
            limiter.wait(url)
        except NotAskedError as exc:
            if not _not_asked(exc, pending[i:] if isinstance(exc, DeadlineReachedError) else [row], hook, progress):
                continue
            break
        with log_stage(storage, stage="m10_crawl", method=f"{economy}:fetch", input_data=url) as rec:
            try:
                fr = fetch(url)
            except NotAskedError as exc:
                # Not a failure of the Portal's: the row stays pending.
                rec.decision = f"not asked: {type(exc).__name__}"
                if not _not_asked(exc, pending[i:] if isinstance(exc, DeadlineReachedError) else [row], hook, progress):
                    continue
                break
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                storage.manifest_mark_failed(url, error=error[:500])
                rec.decision = f"failed: {type(exc).__name__}"
                report.failed += 1
                progress(f"M10 crawl | FAILED {type(exc).__name__}: {url}")
                hook.skipped(url, "fetch_error", skip_reason("fetch_error", type(exc).__name__))
                continue
            if fr.http_status >= 400:
                storage.manifest_mark_failed(
                    url, error=f"HTTP {fr.http_status}",
                    http_status=fr.http_status, method=fr.method,
                )
                rec.decision = f"failed: HTTP {fr.http_status}"
                report.failed += 1
                progress(f"M10 crawl | FAILED HTTP {fr.http_status}: {url}")
                hook.skipped(
                    url, "http_error", skip_reason("http_error", f"HTTP {fr.http_status}")
                )
                continue

            sha = hashlib.sha256(fr.content).hexdigest()
            rec.output_data = fr.content
            original = storage.manifest_find_original(sha)
            if original is not None and original["url"] != url:
                storage.manifest_mark_fetched(
                    url, http_status=fr.http_status, method=fr.method, sha256=sha,
                    content_type=fr.content_type, size_bytes=len(fr.content),
                    local_path=original["local_path"], is_duplicate_of=original["url"],
                )
                rec.decision = "duplicate"
                report.deduplicated += 1
                progress(f"M10 crawl | duplicate of {original['url']}: {url}")
                hook.skipped(url, "duplicate", skip_reason("duplicate"))
                continue

            raw_dir.mkdir(parents=True, exist_ok=True)
            local = raw_dir / f"{sha[:12]}_{_safe_name(row['filename_hint'], url)}"
            local.write_bytes(fr.content)
            storage.manifest_mark_fetched(
                url, http_status=fr.http_status, method=fr.method, sha256=sha,
                content_type=fr.content_type, size_bytes=len(fr.content),
                local_path=storable_local_path(local, data_dir),
            )
            rec.decision = f"fetched via {fr.method}"
            report.fetched += 1
            progress(
                f"M10 crawl | fetched via {fr.method} ({len(fr.content):,} bytes,"
                f" {fr.content_type or '?'}): {url}"
            )
            hook.fetched(url, len(fr.content), fr.method)

    return report
