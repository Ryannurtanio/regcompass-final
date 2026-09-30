# Portal adapter reference

Everything the M10 crawl learned about each country portal, recorded so the next
adapter (a new economy at the finale, or a re-crawl after a portal change) does not
rediscover it. Every fact below was verified live; dates note when. The committed
evidence trail is `tests/golden/m10/manifest.json` (urls, hashes, methods) plus the
regression lanes in `tests/test_crawl.py`.

The general lesson that repeats across portals: **trust the link the portal itself
renders to users over its structured data fields.** Both non-trivial quirks below
(AU volume numbering, MY doubled path) were cases where a "cleaner" metadata field
lied and the portal's own working link told the truth.

## Singapore - sso.agc.gov.sg (strategy: curl_cffi ladder)

- **The WAF is a TLS-fingerprint filter, not a JS wall.** Naive clients get HTTP 403
  on both browse and act pages (verified 4 Jul 2026). `curl_cffi` with
  `impersonate="chrome"` clears it with no browser process (verified 5 Jul 2026):
  act pages and the 1.8 MB browse index both answer 200. JavaScript rendering is
  NOT required; the act HTML is server-rendered.
- **Act URL shape:** `https://sso.agc.gov.sg/Act/{code}` where `{code}` is the SSO
  short code (TA1999, PDPA2012, CA2018, CMA1993, ETA2010, CoA1967, BA1970,
  ITA1947, EmA1968, CPC2010 all verified 5 Jul 2026).
  Browse index: `/Browse/Act/Current/All?PageSize=500`.
- **QUIRK - the act HTML page is NOT the whole act.** `/Act/{code}` lazy-loads
  its provisions through `/Details/GetLazyLoadContent` (Knockout JS, ajaxToken);
  the served HTML carries front matter + the table of provisions and only part
  of the body for large acts. Verified 5 Jul 2026: the PDPA page contains the
  s.26 HEADING but not its body; the Companies Act page lacks s.199 entirely.
  An HTML crawl looks complete (200, sizeable page, act title present) while
  silently missing most of the statute - byte integrity checks do not catch it.
- **The whole-act document is `/Act/{code}?ViewType=Pdf`:** the official
  consolidation PDF, born-digital (PDPA: 124 pages, zero low-yield pages, 100%
  word alignment), served with the same TLS-fingerprint WAF rules as the HTML.
  This is the M10/M11 byte source for SG; the HTML act pages are recorded in
  the manifest as kind='index' (navigation, not corpus).
- **Escalation ladder** (each rung smoke-tested, never assumed): (1) curl_cffi,
  (2) plain headless Playwright using `response.body()` (still raw server bytes),
  (3) Patchright headful - deliberately
  unwired while rungs 1-2 work. A 403/429/503 escalates; a 404 is final (it means
  the same thing on every rung).
- Politeness: robots.txt publishes `crawl-delay: 6` (verified 6 Jul 2026 via curl_cffi;
  a plain fetch gets the CloudFront 403, which is how an earlier note here wrongly
  recorded "publishes none" - never record "publishes none" when the tool could not
  see the source). The crawler now fetches robots.txt at run time through the same
  fetch strategy and enforces max(config floor, published delay); the config floor
  is 6s (`config/crawl_seeds.yaml`). Disclosure: the shipped 5 Jul corpus crawl ran
  at the old 5s floor against the published 6s ask; found and corrected 6 Jul.

## Australia - legislation.gov.au (strategy: register OData API over httpx)

- **The SPA's own public API does everything; no browser needed on the happy path.**
  Base: `https://api.prod.legislation.gov.au/v1`. Discovery chain (all plain httpx,
  verified 5 Jul 2026):
  1. Title search: `/titles?$filter=contains(name,'{query}')&$select=id,name,collection,status`.
     Escape single quotes by doubling them (OData). Match the register name EXACTLY
     (casefold): a fuzzy pick can silently crawl the wrong statute.
  2. Latest version: `/versions/find(titleId='{id}',asAtSpecification='latest')?$expand=documents`
     returns registerId (the compilation, e.g. C2026C00243), compilationNumber,
     start/retrospectiveStart dates, and the documents array (format, volumeNumber,
     sizeInBytes, isAuthorised).
  3. Download URL (deterministic, discovered by click-sniffing the SPA's downloads
     page with Playwright): 
     `https://www.legislation.gov.au/{titleId}/{start}/{retrospectiveStart}/text/original/pdf/{volumeNumber}`
     with dates as YYYY-MM-DD. Content-disposition returns the canonical filename
     (`{registerId}VOL01.pdf` etc.). Content-length matches the API's sizeInBytes
     exactly - use that as a transfer check.
- **QUIRK - volume 0:** single-volume acts carry `volumeNumber: 0` and the download
  URL takes the LITERAL 0 (`.../pdf/0` answers 200; regression: coercing 0 to 1
  404'd the Privacy Act 1988 and Online Safety Act 2021 on 5 Jul 2026). Multi-volume
  compilations number 1..n. `.../pdf` without the suffix also works for volume 0 but
  the literal form is uniform, so use it.
- **QUIRK - title ID vs register ID:** API routes and site routes take the TITLE id
  (C2004A04868). Register/compilation IDs (C2026C00243) 400/404 on the API and
  soft-404 on site routes ("The requested title could not be loaded"). The register
  ID appears only in version metadata and download filenames.
- **robots.txt asks a 10 second crawl delay** (verified 4 Jul 2026); honored as the
  AU politeness floor. The sitemap.xml is example/navigational only (28 entries,
  self-labelled) - never a discovery source.
- Playwright stays available as the fallback and the discovery tool (it found the
  download URL shape); the judge never runs it.

## Malaysia - lom.agc.gov.my (strategy: fess search proxy over httpx)

- **The printed portal host is dead.** Organizer materials print `lor.agc.gov.my`:
  that host does not resolve (DNS verified 4 Jul 2026). The live portal is
  `lom.agc.gov.my`. Keep this correction visible for judges cross-checking.
- **BROKEN 16 Sep 2026: the search proxy now returns an ENCRYPTED payload.** One
  recorded live request (identified user agent, query "personal data protection")
  answers HTTP 200 with `{"encrypted": true, "data": "<base64>"}` instead of the
  Solr envelope below. The `response.docs[]` shape is no longer on the wire, which
  is the whole reason Malaysian Discovery returns zero hits today. The ciphertext
  (21,254 bytes, not block-aligned, no OpenSSL salt header) cannot be read without
  the key the portal's own page script holds. The recorded answer is
  `tests/fixtures/portals/my/fess_search_personal_data_protection_2026-09-16.json`
  and `discover_my` now NAMES this envelope in its miss instead of reporting an
  empty result set. Reading the payload needs the page script, which is a separate
  fetch and a separate decision; the paragraphs below describe the shape the
  shipped corpus was fetched through and still apply once the payload is readable.
- **The browse index is not scrapable; the search proxy is.** `principal.php` pages
  render a DataTables shell with no act rows in the HTML (the only php endpoints in
  the page generate a print PDF and count hits). The real data source is
  `https://lom.agc.gov.my/fess-proxy.php` (a Fess/Solr search proxy) returning JSON
  at `response.docs[]` (verified 5 Jul 2026). Query params:
  `q` (quote the phrase), `fq=""`, `start`, `rows`, `sort=publicationDate desc`,
  `lookup=all`, `kategori`, `draw=1`.
- **QUIRK - kategori must include amendment acts.** Principal-act categories are
  `iagcact,iagcact_repealed,iagcact_revised,iagcact_translated,iagcact_updated`;
  amendment acts live ONLY under `iagcact_amendment`. The ESCAP database itself
  cites amendment acts as evidence (MY 7.4 rests on the PDPA (Amendment) Act 2024,
  A1727), so the crawl queries include it.
- **QUIRK - trust the anchor, not the GENERATEPDF fields.** Each doc carries both
  pre-built anchors (`DOC2DOWNLOADBI` / `DOC2DOWNLOADBM`, an `<a href>` fragment)
  and structured fields (`DOC2DOWNLOADBI_GENERATEPDF` = JSON `[{path, docName}]`).
  For some acts the structured `path` carries a doubled prefix
  (`/upload/portal/akta/outputaktap/upload/portal/akta/outputaktap/600_BI/`) that
  HTTP 500s - observed live 5 Jul 2026 on ACT 588 - while the anchor href is
  correct. The portal's own search UI has the same bug (it builds links from the
  structured fields). Adapter rule: parse the anchor href first (URL-encode its raw
  spaces), fall back to path+docName only when no anchor exists.
- **BI/BM selection:** prefer the English PDF (BI), fall back to Malay (BM - the
  M2 OCR and M3 gloss lanes handle it). PDFs live under
  `https://lom.agc.gov.my/ilims/upload/portal/akta/...`.
- **Known gap:** the Electronic Commerce Act 2006 (Act 658) has NO download fields
  in the search index at all (verified 5 Jul 2026) - a portal data gap, recorded as
  a crawl miss. It is not an ESCAP-cited law for MY pillars 6-7.
- **Known gap 2:** the principal Income Tax Act 1967 (Act 53) has no entry with a
  PDF in the search index either (verified 5 Jul 2026): "income tax" surfaces only
  amendment acts and the Petroleum (Income Tax) Act 1967 (Act 543 - a DIFFERENT
  act; do not mistake its "No. 45 of 1967" doc name for the ITA). The Round 1
  Database cites Act 53 for MY 6.2/7.3; recorded as not_available in
  config/round1_corpus_map.json.
- **Naming trap:** the portal's official BI title of Act 807 is "SERVICE TAX ACT
  2018" (singular), while the ESCAP database prints "Services Tax Act". Match by
  act number, not by name alone.
- **QUIRK - principal-act PDFs can be OUTDATED revisions.** The Criminal
  Procedure Code (Act 593) PDF in the index is a pre-2012 revision: it lacks the
  s.116B-116C computerized-data access provisions the ESCAP database relies on
  (verified 5 Jul 2026 - zero occurrences of "computerized" or even "access to"
  in its 490k OCR chars). The cited text lives in the amendment acts the portal
  DOES serve (A1431 inserted it). Adapter rule: crawl the amendment acts
  alongside principal acts (the kategori list already includes
  iagcact_amendment) and treat principal + amendments as jointly representing a
  citation (config/round1_corpus_map.json does this for the CPC).
- Politeness: no retrievable robots.txt (the URL returns HTTP 500, checked 6 Jul 2026);
  our floor is 5s, and the runtime robots.txt check honors any ask the portal later
  publishes. Since 16 Sep 2026 that 500 is read as "unavailable" rather than "no
  rules", and Malaysia carries `robots_unreachable_since: 2026-07-06`: more than
  30 days, so the crawl runs under RFC 9309's 30-day rule and every Malaysian
  Discovery record says so in its `robots` line.

## India - indiacode.gov.in (strategy: dspace_rest)

Verified 16 Sep 2026. Recorded bytes and the request log:
`tests/fixtures/portals/in/README.md`.

- **The Portal moved.** The host the organizer materials print,
  `www.indiacode.nic.in`, refuses the identified agent with an Akamai HTTP 403 on
  both `/robots.txt` and `/`. Because a WAF fingerprint is not a published rule,
  the documented escalation rung (curl_cffi) was tried once, and what it returned
  was a migration notice: India Code now lives at `indiacode.gov.in`. The new host
  answers the identified agent, so nothing here impersonates a browser.
- **It runs DSpace 7 and publishes a read-only REST API**, which is what the
  adapter reads: `GET /server/api/discover/search/objects?query=<exact
  title>&dsoType=item&size=20&embed=bundles/bitstreams`. The `embed` is the whole
  trick: one request per seed returns the search hit AND the download URL, so
  neither the item nor its bundles need a second call.
- **Every SECTION of an act is its own item** ("Application of Act.", "Right to
  nominate."), and a section carries no PDF. Only an exact title match may become
  a Document, as on the Australian register; a seed matching nothing is a recorded
  miss, never resolved to the nearest hit. Titles are normalised for the leading
  article, a trailing full stop and whitespace only, because India Code prints
  "The Digital Personal Data Protection Act, 2023." with the stop and others
  without it.
- **The Document is the ORIGINAL bundle's PDF** (`.../core/bitstreams/<uuid>/
  content`, `Content-Type: application/pdf`). DSpace also keeps a TEXT bundle,
  which is its own extraction of that PDF: it is not the source and must never
  become the quote source.
- **One exact title can match several items** (the Information Technology Act,
  2000 appears more than once). Each is emitted; identical bytes collapse in the
  crawl loop's sha256 dedupe and a genuine difference is something a reviewer
  should see.
- Politeness: `/robots.txt` answers HTTP 500, and HTTP 502 on a retry the same
  day. That is "unavailable", not "no rules", so the default rule would refuse
  India until 2026-10-17, the day the 30-day clock started by
  `robots_unreachable_since: 2026-09-16` runs out. This Portal instead carries
  `robots_unavailable_policy: proceed`, an operator decision of 17 Sep 2026:
  **Discovery and the add-by-URL lane go ahead for India**, at our 2 s floor
  and under every other rule (the host whitelist, the identified agent, and
  the published rules the moment the Portal can serve them). Both the status
  robots.txt answered with and the policy that let the request out go onto the
  record. Deleting that one configuration line returns India to the default
  refusal.

## Thailand - searchlaw.ocs.go.th (strategy: manual, operator-supplied)

Verified 22 Sep 2026 across 24 recorded requests. Evidence, with the verbatim
answers: `tests/fixtures/portals/th/README.md`.

**The printed host is dead, and the live one has a good certificate.**
`www.krisdika.go.th`, the Council of State host the plan named, serves a
self-signed certificate and answers 404 on every path. The live law library is
`https://searchlaw.ocs.go.th/council-of-state/`, whose certificate verifies
cleanly against certifi (GlobalSign OV, `*.ocs.go.th`, valid to 2 Nov 2026). So
the planned certificate pinning is not needed and was not added: TLS
verification stays on everywhere, and `tests/test_thailand_portal.py` asserts
that no file under `src/` disables it. `www.law.go.th` answers Cloudflare 403
and the Royal Gazette blocks bots; both are out of scope and neither was
requested. `/robots.txt` answers 404, so no rules are published and our own
2.0 s floor is the whole promise.

**The interface.** An Angular application over an unauthenticated JSON API at
`/ocs-api/{service}/{controller}/{method}`, here always service `public` with
controllers `home`, `doc`, `browse`, `comment`, `proc`, `banner`. Every call is
a POST of one envelope:

```json
{"reqHeader": {"reqId": "<epoch ms>", "reqChannel": "WEB",
               "reqDtm": "yyyy-MM-dd HH:mm:ss.SSS", "reqBy": "unknow",
               "serviceName": "<service>", "uuid": "<uuid4>",
               "sessionId": "<uuid4>"},
 "reqBody": {...}}
```

Every answer is HTTP 200 and carries its outcome in `respHeader.errorCode`.
`reqBy` must be non-empty; `unknow` is the application's own value for an
anonymous visitor, and `""` is refused by name. One browse service,
`getPublicYears`, fails with the generic processing error PYC_9998 even with
that value; the recorded answer is `browse_years_error_2026-09-22.json`.

**Discovery is not wired, and this is why.** The browse lane answers us:
`getPublicTags` returns the 43 subject tags, and `getPublicFoldersByTag`
returns the Acts under one, naming exactly the statutes this project wants
(Electronic Transactions B.E. 2544, Cybersecurity B.E. 2562,
Computer-related Crime B.E. 2550, Digital Economy and Society B.E. 2560,
Digital Government B.E. 2562). Everything that would turn one of those into a
Document fails:

- all three search services answer the same `6013` code under their own
  prefix, `PSL_6013` for `searchPublicLaw`, `PST_6013` for
  `searchPublicByTag`, `NON_6013` for `suggest`, for every body tried,
  including the minimal one and an ASCII keyword;
- both law-text services answer a cipher error, `PDL_CIPHER_EXCEPTION` for
  `getPublicLawDoc` and `PPH_CIPHER_EXCEPTION` for `printPublicLawAsHtml`.

A document address is `/council-of-state/#/public/doc/:timelineId`, and a
`timelineId` is issued only in a search result, so with the search lane down
there is no identifier to open a law with, and the browse lane's `indexId` is
not one. The bodies sent are the application's own, read out of its bundle, and
the browse lane answered `SUCCESS` to the same envelope on the same connection
minutes earlier, so this is the Portal's condition rather than our client. The
application performs no client-side encryption on either document call: it
posts the plain envelope exactly as the browse services do.

**The file-download route is never used.** `/ocs-api/file/downloadFile/{uuid}`
takes an AES-encrypted `reqHeader`, and nothing here works around that.

**Source URL rule, for the day the Portal recovers.** A Thailand Source URL is
an address on `searchlaw.ocs.go.th` that a person can open and see that law,
which is the application's own document route above, never an API endpoint and
never a file-download URL. The English versions the Council of State's
translation centre publishes are labelled unofficial: such an address may be
recorded in a Document's notes as a secondary reference, exactly as the Lao
English renderings are, and is never the Source URL and never the text a
verbatim quote is taken from.

Documents therefore arrive through "Add document". The host IS whitelisted, so
an operator-supplied `searchlaw.ocs.go.th` Document needs no override. Thai
traineddata is vendored, so a Thai scan reads normally.

Added 29 Sep 2026: the Ministry of Digital Economy and Society (`mdes.go.th`)
answers `/law/detail/{id}` with the law's PDF, its robots.txt allows everything
and its certificate verifies. It carries only the laws in its own remit. Seeded
in `config/crawl_seeds.yaml` for a Discovery by Pillar: 3577 Personal Data
Protection Act, 3616 Electronic Transactions Act and 3618 Computer-related Crime
Act, each the government's unofficial English translation (Language English).
The Thai PDPA PDF (3541) has a broken text layer and is not seeded.
`www.law.go.th` and the Royal Gazette are never requested.

## Viet Nam - vbpl.vn (strategy: manual, operator-supplied)

Verified 16 Sep 2026. Evidence: `tests/fixtures/portals/vn/README.md`.

The Portal publishes real rules (`Allow: /`, `Disallow: /api/`,
`Disallow: /Pages/`) and a sitemap, but the sitemap is four static navigation
pages, so there is no law URL to discover from it. The document list exists only
behind `/api/`, which those rules forbid, and the pages are a Next.js shell
behind a JavaScript bot-defence script. A headless browser would make the
forbidden `/api/` calls itself, so Discovery is not wired here. The host IS
whitelisted, so an operator-supplied `vbpl.vn` Document needs no override.

Added 29 Sep 2026: the Official Gazette. `congbao.chinhphu.vn` serves each
document page (`/van-ban/{slug}-{id}.htm`) as server HTML with metadata only;
the law is the `congbaocdn.chinhphu.vn` PDF that page links, with a real
Vietnamese text layer, as promulgated, or the consolidated text (VBHN) where
the National Assembly Office published one. Seeded by PDF address, at least one
law for each of the twelve Pillars, among them the Law on Personal Data
Protection 91/2025/QH15 with Decree 356/2025/ND-CP and the Law on Cybersecurity
116/2025/QH15. Decree 13/2023/ND-CP and the Law on Cybersecurity 24/2018/QH14 are
no longer in force and are not seeded. `vanban.chinhphu.vn` is not used: its
files are scanned images.

## Kazakhstan - adilet.zan.kz (strategy: manual, operator-supplied)

Verified 16 Sep 2026. Evidence: `tests/fixtures/portals/kz/README.md`.

The most explicit robots.txt of any Portal we have read: act pages open, `/api/`,
`/search` and `/advanced-search` closed, the whole site closed to model-training
crawlers by name, and search-and-citation agents deliberately left open.
RegCompass is not named, so the `*` group binds us. The act route answers a 2.7 KB
shell whose own comment states that the full text is withheld from non-browser
clients on purpose, so that the system is cited rather than drained in one
command. A headless browser could render around that; honouring it is the reason
Kazakhstan is operator-supplied. The sitemap its robots.txt advertises answered
HTTP 504 on both attempts. Host whitelisted; Russian is the default Language.

Added 29 Sep 2026: ILO NATLEX (`natlex.ilo.org`), an intergovernmental repository
of official texts. Its download address
`/dyn/natlex2/natlex2/files/download/{isn}/KAZ-{isn}.pdf` answers with the PDF
and robots.txt allows it; its detail pages sit behind a browser check, so they
are never browsed and only seeded addresses are fetched. Seeded: 96710, Law No.
94-V On Personal Data and their Protection, the Ministry of Justice's unofficial
English translation, and 108188, the Entrepreneurial Code No. 375-V, in the
unofficial English translation NATLEX holds (Language English for both).

Also added 29 Sep 2026: the Eurasian Economic Commission (`eec.eaeunion.org`,
over https), whose robots.txt allows `/upload/`. Its own PDFs of the Union acts
that bind Kazakhstan are seeded: the Treaty's Annex 8, Section XXII with Annex
25, Section X and Annex 9, Board Decision No. 30 with its Annex 9, and the
Customs Code. English is Kazakhstan's third Language, so a NATLEX translation
added by hand can carry it.

## Mongolia - legalinfo.mn (strategy: manual, operator-supplied)

Verified 16 Sep 2026. Evidence: `tests/fixtures/portals/mn/README.md`.

No robots.txt is published (the URL answers 404 with the site's 404 page), and
the site returns real HTML, but no act list is ever in it: the category pages and
the search page are furniture, the search itself is handed to a client-side
Google widget, and the lists arrive through an undocumented POST endpoint
(`/mn/ajaxList/`) returning pre-rendered markup. The page that looks like an API
reference (`/api/front/index.html`) is an article about the Constitution. Nothing
here is a published interface to build an adapter on. Host whitelisted.

Added 29 Sep 2026: a law page, `/mn/detail?lawId={id}`, is whole server HTML in
UTF-8, so laws are seeded by that address: Personal Data Protection
(16390288615991) and Cybersecurity (16390365491061).

## Russian Federation - pravo.gov.ru, kremlin.ru over http (strategy: manual)

`tests/fixtures/portals/ru/README.md`. Three attempts on 16 Sep 2026, up to a 90 s
timeout, all ending at the TCP connect with no HTTP exchange. Corrected 22 Sep
2026: port 443 is blocked on our network path to Russian government hosts, and
port 80 answers. Over HTTP the Portal has an open API, but its acts are
image-only scanned PDFs and it publishes amendments rather than consolidated
law, so the former Thailand fallback was examined and not taken.

Added 29 Sep 2026: two official hosts, whitelisted and asked over plain http
only (`http_hosts` in `config/portals.yaml`), robots.txt read over the same
scheme, 60 s timeout. `kremlin.ru` (the President's acts bank):
`/acts/bank/{id}/print` is the whole current text, and robots.txt allows
`/acts/bank/`. `pravo.gov.ru` (the Official Internet Portal of Legal
Information): `/proxy/ips/?doc_itself=&nd={nd}&page=1`. Seeded: Federal Law No.
152-FZ On Personal Data (kremlin.ru 24154 and pravo.gov.ru nd 102108261) and
Federal Law No. 149-FZ On Information (nd 102108264). Later the same day:
kremlin.ru print pages for nine more federal laws the 2025 baseline cites, and
the Eurasian Economic Commission (`eec.eaeunion.org`, see Kazakhstan, asked over
https) for the Union acts that bind the Russian Federation.

## China - www.cac.gov.cn (strategy: manual, add by URL)

Checked 23 Sep 2026, supervised, 16 requests in all under the identified agent,
robots.txt first on every host and at least 3 s between two requests to one host.
China's statutes come from the **Cyberspace Administration of China**
(`www.cac.gov.cn`), the regulator that enforces all three: it republishes the
National People's Congress text in full and credits the source on each page. Its
robots.txt addresses every agent and closes only `/zfz/`, `/wxb_zfz/`, `/wxzf/` and a
video player path, so the law pages are open; no crawl-delay is published, so our own
3 s floor stands. The host is whitelisted and Discovery is not wired: the Corpus is
three fixed URLs, each added by its Source URL through "Add document".

| Law | Source URL |
| :---- | :---- |
| Personal Information Protection Law (2021, 74 articles) | https://www.cac.gov.cn/2021-08/20/c_1631050028355286.htm |
| Data Security Law (2021, 55 articles) | https://www.cac.gov.cn/2021-06/11/c_1624994566919140.htm |
| Cybersecurity Law, as amended by the NPC Standing Committee decision of 28 Oct 2025, in force 1 Jan 2026 (81 articles) | https://www.cac.gov.cn/2025-12/29/c_1768735112911946.htm |

Each is one UTF-8 HTML page carrying the complete text; the page's navigation and
footer stay in the extracted stream, which is harmless because every quote is verified
against that same stream. The 2016 Cybersecurity Law text (79 articles) is superseded
by the consolidated one above.

**The National People's Congress hosts are not used.** `flk.npc.gov.cn`, the national
law database, opens its robots.txt with a line forbidding any automated tool, script or
crawler from collecting or copying site data, then `User-agent: *` / `Disallow: /`. It
is never whitelisted and never requested; its detail pages are a JavaScript shell in
any case. `www.npc.gov.cn` refused the HTTPS handshake on every client we tried (TLS
1.2 and 1.3), and over plain HTTP its bodies stalled past our 120 s fetch timeout on
two of the four pages requested, so it cannot be relied on.

**The Chinese lane, verified 22 Sep 2026 on the team's scan of the Personal Information
Protection Law** (image-only, 7 pages; their Cybersecurity Law and Data Security Law scans
are the same shape and were not run. None of the scans is in this repository; the OCR
excerpt the tests read is `tests/fixtures/ocr_reference/pipl_cn_chapter1_excerpt.txt`):

- **Language of Source is `Chinese`**, the Portal's single configured Language and one of
  the organizer's eleven dropdown values, so a Chinese row never falls through to "Other".
- **OCR.** No Chinese traineddata is vendored, so tesseract reads the pages as English and
  the quality verdict forces manual review on that ground alone. RapidOCR escalates and
  reads them properly on its own Chinese model: 7,942 clean characters out of the
  Protection Law. The English dictionary proxy is skipped rather than reported as zero,
  and tesseract's own word confidence over the misread glyphs (0.95) is exactly the
  number the forced-review rule exists to distrust.
- **Gate.** Chinese is absent from `KEYWORD_TIER_LANGUAGES`, so the Gate takes the
  meaning-only lane. It must: `bm25s` tokenizes on whitespace, and Chinese does not
  separate words.
- **Chunking.** Chinese heads a provision with `第N条`, the number written in Chinese
  numerals, and groups provisions under `第N章` chapters. The `article_zh` style profile
  reads both; the numeral becomes an integer so `第十条` sorts before `第十二条`, and a
  chunk's label carries its chapter (`Chapter 2 s. 13`). Without it the whole statute
  degrades to one chunk of kind "other", which the Gate never looks at, and the Economy
  produces nothing at all.
- **Gloss.** The quotes are Chinese, so every row carries an English rendering in the
  appended `Verbatim English` column, labelled non-authoritative. The offline model
  (opus-mt-mul-en) does render Chinese, loosely: the gist survives and the wording
  rambles, which is what the label is for. A quote it cannot render at all gets the
  explicit statement that no rendering is available, never a blank cell.

## Lao PDR - laoofficialgazette.gov.la (strategy: httpx, the gazette's own grid)

Verified 16 Sep 2026 across ten recorded requests; the bodies and the request
log are `tests/fixtures/portals/la/` and its README, and
`tests/test_lao_portal.py` replays them.

- **QUIRK - robots.txt is a soft 404.** `/robots.txt` answers HTTP 200 with
  135,911 bytes of the site's own homepage. That is no published ask: `parse_robots`
  recognises an HTML body and returns the empty policy, so nothing is read as a
  permission and nothing as a refusal. Our own 2.0 s floor stands alone, and a real
  robots.txt this host later publishes is honoured exactly as Singapore's is.
- **The search IS the listing grid.** `https://laoofficialgazette.gov.la/index.php?r=site/index`
  renders a Yii `CGridView` (`id="homelegal-grid"`) over the instruments in force
  (1,481 on the day). Its own filter input `Document[title]` matches anywhere in
  an instrument's Lao title, and `Document_page=N` pages the results ten at a time.
  So one Lao word per source family replaces a search API: ເອເລັກ (elec-) reached 17
  instruments, ໄຊເບີ (cyber) 1, ຄອມພິວເຕີ (computer) 4, ຜູ້ຊົມໃຊ້ (consumer) 5,
  ໂທລະຄົມ (telecom) 11. Cybersecurity takes two words because the Portal titles
  the 2025 security law with ໄຊເບີ and the cyber crime law with ອາຊະຍາກຳທາງລະບົບ
  ຄອມພິວເຕີ.
- **QUIRK - the page carries a second, look-alike table.** Below the grid sits an
  unfiltered table of recent publications with identical row markup. A parser
  reading the page rather than the grid element reports ten phantom results on
  every filter. Everything the adapter reads is scoped to `homelegal-grid`.
- **QUIRK - no pager when the results fit one page.** The walk is driven by the
  grid's own summary line ("ສະແດງ 1-10 ຂອງ 17": showing 1-10 of 17), not by the
  pager, which the Portal omits entirely for a single-page result and renders a
  second time for the other table. `max_pages` in `config/crawl_seeds.yaml` bounds
  the walk.
- **Two PDF columns: `PDF ອັງກິດ` (English) and `PDF ລາວ` (Lao).** The Lao file is
  the official text and the ONLY quote source. Where an English rendering exists,
  its URL is recorded on the Document's Corpus row (`documents.notes`), which is
  where a submission's Notes can cite it as a secondary reference; the export
  does not read that column yet. The columns are read by their header words, not
  by position or filename: the Consumer Protection law's two files are named the
  other way round (`Law on Consumer Protection.pdf` is the file in the ENGLISH
  column), so a filename heuristic would swap the official text for a translation.
- **The instrument's own page adds nothing.** `/index.php?r=site/display&id=NNNN`
  carries exactly the PDF link its grid row already gave (verified on id 2023, the
  Law on Electronic Transactions (Amended)). Discovery therefore reads the listing
  and stops, instead of spending one request per instrument.
- **Document URLs are `/kcfinder/upload/files/<name>.pdf`**, and the names are the
  Portal's own: Lao script, spaces and parentheses all appear in them, so every
  href is percent-encoded once and the already-encoded parts are left alone.
- Politeness: no published ask, our floor is 2.0 s, one connection, identified
  user agent, no impersonation rung (the host answers us as ourselves).

## Indonesia - peraturan.bpk.go.id (strategy: curl_cffi ladder)

Verified 16 Sep 2026 over 13 recorded requests (15 to this host for the day,
counting the two of the read-only research pass); every response body is
committed under `tests/fixtures/portals/id/` with a per-request ledger in its
README. The only edit to those bytes is the ASP.NET anti-forgery token value in
each page, replaced with a placeholder so no per-session secret is committed.

- **The originally intended Portal, `peraturan.go.id`, is UNREACHABLE from our machines.**
  Three attempts (45 s, 90 s and 20 s timeouts) all died at the TCP connect, so
  nothing is known about it from here. This is a fact about our network path,
  not a refusal by the Portal. It stays on the whitelist in
  `config/portals.yaml` because it is an official Indonesian Portal and a
  Document a reviewer adds by hand from it must clear the host check; Discovery
  never points at it.
- **The Portal we use is the Audit Board's national regulation database**
  (JDIH BPK). It is the complete national database, it publishes robots.txt,
  and it answered.
- **Published rules permit us; the edge refuses us.** robots.txt forbids only
  `/Admin/`, `/Identity/`, `/Account/`, `/Manage/`, `/health` and `/Error`, and
  publishes a sitemap. `/Search`, `/Details/` and `/Download/` are all allowed,
  and no crawl-delay is published. The Cloudflare edge nonetheless answers the
  identified user agent with 403 and a "Just a moment..." interstitial, on the
  landing page and on `/Search` alike. So this Economy takes the SAME ladder
  Singapore takes: ask as ourselves, escalate to the curl_cffi rung only after
  the refusal, and record `escalated_to_impersonation: true` on the Discovery
  record. The judgment being made is the one already made for Singapore, and it
  is disclosed the same way.
- **Document = `/Download/{file id}/{file name}.pdf`**, the statute's own PDF.
  The `/Details/{id}/{slug}` page carries the law's metadata, its abstract and
  its download link, but NOT the articles, so it is not the Document. Since
  29 Sep 2026 Discovery drops a `/Details/` address (and a `jdih.komdigi.go.id`
  or `jdih.kominfo.go.id` `/produk_hukum/view/` page) before any request; a law
  left with no other address is reported as not fetched, reason `summary_page`.
- **QUIRK - the download id is not the detail id.** Law 27/2022 is
  `/Details/229798/uu-no-27-tahun-2022` and `/Download/224884/UU Nomor 27 Tahun
  2022.pdf`. A Document URL therefore cannot be computed from a detail id; it
  has to be read off the Portal. That is why the seeds carry the download
  reference in full and why the recorded listings are committed beside them.
- **QUIRK - file names are served with double spaces.** The 2016 amendment is
  `UU Nomor  19 Tahun 2016.pdf`. `discover_id` percent-encodes and nothing else;
  normalising the name 404s.
- **Search is server-rendered, but Discovery does not use it.** The listing
  pages carry the result count, the `/Details/` links and the `/Download/` links
  in the response bytes, which is how the seeds were resolved. Discovery itself
  asks the Portal nothing beyond robots.txt and the Documents: seeding is by
  source family, and a search that needed the impersonating rung to run at all
  would put the escalation on the discovery path rather than only on the fetch.
  Restrict a listing with `jenis=8` (Undang-undang); `tentang` matches the
  official title, which spells the data protection law "Pelindungan", not
  "Perlindungan".
- **Hosts NOT used, and why.** `jdih.setneg.go.id` is fully permissive and has
  no WAF, but its listing is fetched by the browser and its only machine-
  readable route is an untested `_next/data` JSON path keyed by a build id,
  which is a private API and a fragile seed; it also publishes Presidential
  instruments and above, so a Government Regulation may be missing.
  `jdih.komdigi.go.id` is reachable and server-rendered, but its robots.txt
  disallows the search path and EVERY download route, so honouring it leaves no
  way to fetch a statute. Neither was requested beyond one robots.txt and one
  landing page during research.
- Politeness: the Portal publishes no crawl-delay, so our own 5 s floor is the
  promise, set in both `config/portals.yaml` and `config/crawl_seeds.yaml`.
- **Open question: text layer or scan.** No statute PDF was ever recorded. One
  request of the budget was a streamed 64 KB head of the Law 27/2022 PDF and it
  died in the client before a body byte was read. Whether BPK's PDFs carry a
  text layer or need OCR is therefore unverified. Indonesian OCR data (`ind`)
  is vendored either way, and the Gate runs the meaning-based tier only for
  Bahasa Indonesia. The operator's supervised Discovery settles it, and
  `prepared` stays false until it does.
- **Not yet seeded**, for want of request budget rather than by choice:
  Law 7/2014 on Trade, and Law 6/2023, which amends the Telecommunications Law.
  Each needs one recorded listing to read its download reference off.
  Government Regulation 71/2019 on Electronic System and Transaction Operation
  was the third such gap and is now seeded, from the `jenis=10` listing.
- **The veto path.** If the escalation is judged unacceptable on this Portal,
  Indonesia becomes `strategy: manual` with one line in `config/portals.yaml`:
  Discovery then refuses by name and every Document arrives through "Add
  document" against the same whitelist. `tests/test_portal_id.py` holds that
  path open.

## Politeness (encoded in src/regcompass/crawl.py)

- **We say who we are.** Every Discovery request goes out under
  `RegCompass/<version> (+https://github.com/Ryannurtanio/regcompass)`, so a portal
  administrator reading their logs can see us and reach us.
- **The browser-impersonating rungs are an ESCALATION, not the opening move.** The
  Singapore ladder now asks first as ourselves over plain httpx; only a WAF refusal
  (403/429/503) escalates to curl_cffi TLS impersonation and then to headless
  Playwright. A Discovery that escalated records
  `escalated_to_impersonation: true` in its report and the command line prints it,
  so the step is never silent.
- **One connection per Discovery**, pooled and capped at one, rather than a fresh
  socket per request.
- **robots.txt Disallow is honoured**, for the group naming our agent token or for
  `*`, with RFC 9309 longest-match precedence between Allow and Disallow. A
  disallowed URL is skipped without a request and counted in the Discovery report.
- **A missing robots.txt and an unavailable one are different answers**
  (RFC 9309 section 2.3.1, corrected 16 Sep 2026; the code previously treated
  every status at or above 400 alike). A 4xx, 404 included, means NO rules are
  published: our configured floor applies alone, and absence of an ask is never
  an ask to hurry. A 5xx means the rules may exist and the Portal cannot show
  them, so we stop. A connection that never reached the Portal is treated as
  the 4xx case: that is our network, not a statement by them.
- **The 30-day rule, which is the rest of that same section.** A Portal whose
  robots.txt has been failing for a long time cannot hold its own texts hostage
  forever, and RFC 9309 says so: after a reasonable period, 30 days in the
  standard's own words, a crawler may treat an unreachable robots.txt as no
  restrictions. We keep that as DATA, not judgement. A Portal carries
  `robots_unreachable_since: YYYY-MM-DD` in `config/portals.yaml`, written by a
  person who checked the Portal by hand on that date. While a 5xx is less than
  30 days old, Discovery and the single-URL add lane both refuse (unless the
  Portal carries the policy in the next bullet), and the refusal names the
  status, the date it started and the date it would lift.
  Once the date is more than 30 days old, both lanes run and the record they
  write carries the sentence `robots: unreachable since <date>, treated as no
  restrictions per RFC 9309 after 30 days`, so nobody has to guess afterwards
  why we crawled a Portal whose rules we never read. A Portal with NO date
  recorded is simply refused, with the message asking for the check to be done
  and the date written down. Uploading a file consults none of this, because an
  upload makes no request.
  Recorded dates today: **Malaysia 2026-07-06** (HTTP 500, so the rule has
  lifted and Malaysian crawls run with this disclosure), **India Code
  2026-09-16** (HTTP 500, then HTTP 502 on a retry).
- **The per-Portal unavailable-robots policy**, which is the one thing that
  overrides the refusal before the 30 days are up. A Portal may carry
  `robots_unavailable_policy: proceed` in `config/portals.yaml`, an operator's
  recorded decision that an unavailable robots.txt on THAT Portal is read as
  publishing no rules. It is per Portal, never global; `refuse` is the default
  everywhere the line is absent, and an unknown value is refused when the
  configuration loads. Proceeding narrows nothing else: the spacing floor, the
  host whitelist, the identified agent and the Portal's own rules whenever it
  can serve them all apply unchanged, and the 30-day clock is untouched for
  every Portal on the default. Nothing about it is silent: the Discovery record
  and the add-by-URL answer both carry `robots_unavailable_status` (what the
  Portal answered) and `robots_unavailable_policy: proceed` (what let the
  request out), beside the `robots` sentence saying it in words. India Code
  carries it; every other Portal is on the default.
- **Spacing floor** = max(the Portal's `min_interval_seconds` in
  `config/portals.yaml`, the seed file's `rate_limit_seconds`, the published
  crawl-delay). robots can raise our politeness, never lower it.

## Cross-portal adapter rules (encoded in src/regcompass/crawl.py)

- Store the EXACT response body bytes; sha256 over those bytes is the dedupe key
  and the ground-truth anchor M1 hashes. Never a DOM, never a re-flow.
- Discovery is seeded by SOURCE FAMILIES (`config/crawl_seeds.yaml`), never by
  feeding indicator questions to a search engine.
- Transport retries are tenacity-capped (3) INSIDE one fetch; across runs a failed
  manifest row is history, not a retry queue. Resume skips fetched rows.
- Block statuses (401/403/429/503) escalate the ladder; 404/5xx are answers,
  recorded as failed.
- Per-host rate limiting always on; floors in `config/crawl_seeds.yaml`.
