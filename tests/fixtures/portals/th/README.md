# Recorded Office of the Council of State answers

Twenty-four requests to `searchlaw.ocs.go.th` on 22 Sep 2026, between 02:11 and
02:20 UTC, all under the identified user agent
`RegCompass/0.1.0 (+https://github.com/Ryannurtanio/regcompass)`, on one
connection, spaced at least 2.0 s apart, robots.txt first. TLS verification was
on for every one of them, against the certifi bundle. **Headless browser: no.**
No 403 and no 429 was seen at any point. Ten of the twenty-four are committed
here; the other fourteen are named at the bottom and were not kept.

Every body here is the verbatim response.

| File | Request | Status, bytes |
|---|---|---|
| `robots_404_2026-09-22.html` | `GET /robots.txt` | 404, 2,187 |
| `app_shell_2026-09-22.html` | `GET /council-of-state/` | 200, 1,303 |
| `browse_tags_1B_2026-09-22.json` | `POST /ocs-api/public/browse/getTags`, `getPublicTags` | 200, 5,880 |
| `browse_folders_tag26_2026-09-22.json` | `POST /ocs-api/public/browse/getFoldersByTag`, `getPublicFoldersByTag` | 200, 10,196 |
| `browse_years_error_2026-09-22.json` | `POST /ocs-api/public/browse/getYears`, `getPublicYears` | 200, 498 |
| `search_law_error_2026-09-22.json` | `POST /ocs-api/public/doc/searchLaw`, `searchPublicLaw` | 200, 399 |
| `search_by_tag_error_2026-09-22.json` | `POST /ocs-api/public/browse/searchByTag`, `searchPublicByTag` | 200, 401 |
| `suggest_error_2026-09-22.json` | `POST /ocs-api/public/doc/suggest`, `suggest` | 200, 391 |
| `law_doc_cipher_error_2026-09-22.json` | `POST /ocs-api/public/doc/getLawDoc`, `getPublicLawDoc` | 200, 346 |
| `print_html_cipher_error_2026-09-22.json` | `POST /ocs-api/public/doc/printHtml`, `printPublicLawAsHtml` | 200, 351 |

Every JSON answer is HTTP 200 with the outcome inside `respHeader.errorCode`,
which is this API's own convention: `SUCCESS`, or a per-service error code.

## The portal printed in the planning materials is dead

`www.krisdika.go.th`, the Council of State host our own plan named, serves a
self-signed certificate and answers 404 on every path. The live law library is
`https://searchlaw.ocs.go.th/council-of-state/`, an Angular application whose
certificate verifies cleanly against certifi (GlobalSign OV, `*.ocs.go.th`,
valid to 2 Nov 2026). **No certificate pinning is needed and none was added**;
TLS verification stays on everywhere, and a test asserts that no source file
disables it. `www.law.go.th` (Cloudflare 403) and the Royal Gazette are out of
scope and were never requested.

## robots.txt is a 404, so no rules are published

The host answers `/robots.txt` with HTTP 404 and a 2,187-byte HTML error page.
Under RFC 9309 a 4xx means no rules are published, which is not the same as the
5xx "unavailable" that stops India. Nothing is disallowed and nothing is
permitted by it: our own 2.0 s floor is the whole promise.

## The interface

The application is driven by an unauthenticated JSON API under `/ocs-api`.
Every call is a POST of one envelope, built by the application itself:

```json
{"reqHeader": {"reqId": "<epoch ms>", "reqChannel": "WEB",
               "reqDtm": "yyyy-MM-dd HH:mm:ss.SSS", "reqBy": "unknow",
               "serviceName": "<service>", "uuid": "<uuid4>",
               "sessionId": "<uuid4>"},
 "reqBody": {...}}
```

Paths are `/ocs-api/{service}/{controller}/{method}`, here always service
`public` with controllers `home`, `doc`, `browse`, `comment`, `proc`, `banner`.
`reqBy` must be non-empty: sending `""` is refused with
`PYC_INVALID_FIELD_REQUIED` naming `req-by`, and the application's own default
for an anonymous visitor is the literal string `unknow`.

## What works, and what does not

**The browse lane answers.** `getPublicTags` with `{"categories": "1B"}`
(`1B` is พระราชบัญญัติ, an Act) returns 43 subject tags with their item counts,
and `getPublicFoldersByTag` with `{"categories": "1B", "tagId": "26",
"orderResult": [...]}` returns the 23 Acts under tag 26
(วิทยาศาสตร์ และเทคโนโลยี, science and technology). Those 23 include the
statutes this project wants: the Electronic Transactions Act B.E. 2544
(`indexId` 598), the Cybersecurity Act B.E. 2562 (199), the Computer-related
Crime Act B.E. 2550 (499), the Digital Economy and Society Development Act
B.E. 2560 (188) and the Digital Government Act B.E. 2562 (627). Both answers
carry `respHeader.errorCode` `SUCCESS`.

**Every search service fails.** All three answer the same `6013` code under
their own service prefix, for every body tried: `searchPublicLaw` gives
`PSL_6013`, `searchPublicByTag` gives `PST_6013`, `suggest` gives `NON_6013`.
This is not our envelope. The same envelope, the same connection and the same
user agent are what the browse services answered `SUCCESS` to minutes earlier,
and the request bodies are the ones the application itself builds, read out of
its own bundle. A Thai keyword and an ASCII keyword fail identically, so it is
not an encoding problem either.

**Both law-text services fail.** `getPublicLawDoc` answers
`PDL_CIPHER_EXCEPTION` and `printPublicLawAsHtml` answers
`PPH_CIPHER_EXCEPTION`, for the bodies the application itself sends
(`{"isTransEng": false, "timelineId": "598"}` and
`{"sectionAndExplains": [], "sectionIds": [], "timelineId": "598",
"isTransEng": false}`). The application performs no client-side encryption on
either call: its document service posts the plain envelope, exactly as the
browse services do. Retrying with `uuid` and `sessionId` in the application's
own UUID v4 shape changed nothing.

## Why no Thai law text is recorded here

The document route is `/council-of-state/#/public/doc/:timelineId`, and a
`timelineId` is issued by the search services, in the `id` field of a search
result. Those services are the only public source of one. With all three of them
answering `6013`, no document identifier can be obtained, and the two services
that would return the text of a law reject the identifier taken from the browse
lane. So there is no reachable path from a seed to a Thai statute today, and
nothing to record. The `6013` and `CIPHER_EXCEPTION` answers are committed here
precisely so that the day they stop being the answer, the tests that pin them
fail and say so.

`tests/test_thailand_portal.py` replays these bytes; no test in the repository
requests anything from this host.

## The fourteen requests not kept

Seven were the application's own JavaScript (`runtime`, and the route and
service bundles behind `/public`), read once to learn the endpoint paths, the
envelope and the request body field names; they are the site's code, not its
answers about law, so they are not committed. The other seven were earlier
probes of the same five services with the wrong `reqBy` or the wrong body, each
superseded by one of the answers above.

Every one of the twenty-four answered HTTP 200, except `/robots.txt`, which
answered 404. The narrowest gap between two of them was 2.05 s.
