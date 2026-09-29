# Recorded India Code answers

Captured live on 16 Sep 2026 under the identified user agent
`RegCompass/0.1.0 (+https://github.com/Ryannurtanio/regcompass)`, one request at
a time, at least 2.0 s apart, except the two escalated ones named below.
**Headless browser: no.** No browser was launched for this Portal at any point,
and no whole PDF was downloaded.

Request count, by host and by rung:

- `www.indiacode.nic.in`, 4 requests: 2 as ourselves over plain httpx
  (`/robots.txt` and `/`, both HTTP 403 from Akamai), then 2 through the
  curl_cffi rung (`/robots.txt` HTTP 404, `/` HTTP 200).
- `indiacode.gov.in`, 12 requests, all as ourselves over plain httpx: robots
  twice (the second a retry, to see whether the first was transient), the
  landing page, the API root, six discover searches, one item, and the 64 KB
  PDF head.

## The Portal moved

`www.indiacode.nic.in`, the host printed in the organizer materials, refused the
identified agent with an Akamai HTTP 403 on both `/robots.txt` and `/`. That is
a WAF fingerprint rather than a published rule, so the documented escalation
rung (curl_cffi TLS impersonation, the same one Singapore uses) was tried once.
It answered, and what it answered is `migration_notice_2026-09-16.html`: the
site has moved to `indiacode.gov.in`. No further request was made to the old
host, and no impersonation is needed on the new one. Both old spellings are on
India's host whitelist in `config/portals.yaml` so that the printed URL an
operator pastes needs no override.

## robots.txt is unavailable, and that is not permission

`indiacode.gov.in` answered `/robots.txt` with **HTTP 500** (application error
page) and, on a retry the same day, **HTTP 502** (nginx bad gateway). Both are
recorded. Under RFC 9309 section 2.3.1.4 a 5xx is "unavailable", which is not
the same as the 4xx "no rules published": the rules may exist and the Portal
cannot show them. `crawl.fetch_robots_policy` therefore raises
`RobotsUnavailableError` and Discovery stops before requesting any Document.

**That refusal is the default, and India overrides it by operator decision.** The
Portal carries `robots_unreachable_since: 2026-09-16` in `config/portals.yaml`,
which starts RFC 9309's 30-day clock, and `robots_unavailable_policy: proceed`,
set on 17 Sep 2026, which lets Discovery and the add-by-URL lane go ahead as if
no rules were published while keeping the spacing floor and the host checks;
every record they write names the robots status and the policy. Remove the
policy line to return to the refusal. The adapter, the seeds and the whitelist
are in place for either outcome.

## Where the interface is documented

- The Portal's own statement of what it serves is recorded in
  `api_root_2026-09-16.json`: the HAL index of `https://indiacode.gov.in/server/api`,
  naming `discover`, `items`, `bundles`, `bitstreams` and the rest. This is the
  DSpace 7 REST API, whose contract is published by the DSpace project at
  https://github.com/DSpace/RestContract (that document was NOT fetched from
  this machine; the evidence committed here is the Portal's own API index).
- **Terms page: none found.** `https://indiacode.gov.in/` is an Angular shell
  (`<ds-app>`) whose HTML carries no links at all, so any terms, privacy or
  end-user-agreement link the site renders is produced by its JavaScript and is
  not visible to a non-browser client. The Portal publishes no robots.txt
  either (see above). What we have, therefore, is a public read-only API index
  served without authentication and no published document restricting its use;
  that is recorded here as exactly that, not as a permission granted.

## Files

- `migration_notice_2026-09-16.html` (2,009 B) - `https://www.indiacode.nic.in/`
  through the escalation rung. The migration notice.
- `robots_500_2026-09-16.html` (148 B) - `https://indiacode.gov.in/robots.txt`,
  HTTP 500, first attempt.
- `robots_502_retry_2026-09-16.html` (150 B) - the same URL on a retry the same
  day, HTTP 502 from nginx.
- `api_root_2026-09-16.json` (8,774 B) - the HAL index of `/server/api`.
- `search_dpdp_2026-09-16.json` (125 KB) - the verbatim answer to
  `GET /server/api/discover/search/objects?query=The Digital Personal Data
  Protection Act, 2023&dsoType=item&size=5&embed=bundles/bitstreams`, which is
  the request `crawl.discover_in` makes for one seed. Five items, of which one
  is the whole Act (ORIGINAL bundle carrying `a2023-22.pdf`, 382,467 bytes) and
  the rest are sections of it or adjacent instruments. The production adapter
  asks for `size=20`; the fixture was recorded at `size=5` to keep it small, and
  the whole-Act item ranks first either way.
- `dpdp_original_head_2026-09-16.pdf` (65,536 B) - the FIRST 64 KB of the file
  the discovered URL returns
  (`/server/api/core/bitstreams/52f9ecbb-b927-4ba6-ae6f-ca2fac50d4df/content`),
  `Content-Type: application/pdf`. Only the head was taken: proving the URL
  answers with the official PDF does not need the whole statute. It is a
  truncated PDF on purpose and cannot be parsed, so the Discovery tests replay a
  short readable stand-in, labelled "test stand-in, not Portal bytes" in the
  document body itself.

## What the adapter does with this

One request per seed, because `embed=bundles/bitstreams` returns the download
URL with the search hit. India Code indexes every SECTION of an act as its own
item ("Application of Act.", "Right to nominate."), and those carry no PDF: only
an exact title match may become a Document, which is why `discover_in` matches
titles the way the Australian adapter does rather than taking the top hit. Every
URL the Portal hands back is checked against the host whitelist before it is
queued, and one that is not on it is dropped and counted on the Discovery record.
