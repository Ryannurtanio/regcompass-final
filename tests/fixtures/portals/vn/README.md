# Recorded Viet Nam Portal answers

Captured live on 16 Sep 2026 from `vbpl.vn` (the National Database of Legal
Documents) under the identified user agent
`RegCompass/0.1.0 (+https://github.com/Ryannurtanio/regcompass)`, one request at
a time, at least 2.0 s apart. Six requests in total: robots, landing, the
sitemap index, the one sub-sitemap, the central-documents listing, and that same
listing URL once more asking for its React flight payload. No PDF was
downloaded, **no headless browser** was run, and `/api/` was never touched.

## Files

- `robots_2026-09-16.txt` (95 B) - the published rules: `Allow: /`,
  `Disallow: /api/`, `Disallow: /Pages/`, plus the sitemap pointer. No
  crawl-delay is published, so our own 2 s floor stands.
- `sitemap_2026-09-16.xml` (207 B) - `https://vbpl.vn/sitemap.xml`. The whole
  index is ONE sub-sitemap, commented "Trang tinh" (static pages).
- `sitemap_0_2026-09-16.xml` (1,218 B) - that sub-sitemap: four navigation URLs
  (home, about, central documents, local documents). No law page is listed.

## Why Viet Nam is operator-supplied

The original plan was sitemap-driven discovery followed by a headless fetch.
The sitemap turns out to name no law, so the only routes to the document list
are:

1. `/api/`, which the Portal's own robots.txt forbids; or
2. rendering the listing in a headless browser, which would make those same
   `/api/` calls itself, and would also work around the JavaScript bot-defence
   script (`/_fec_sbu/fec_wrapper.js`) the Portal loads on every page.

Both are things RegCompass promises not to do, so Discovery is not wired for
Viet Nam. The landing page and the central-documents listing were checked for a
server-rendered document list before that conclusion: both are shells, and the
React flight payload of the listing (requested at the same allowed URL) carries
the page furniture only. The host is whitelisted in `config/portals.yaml` so
that a Document an operator supplies from `vbpl.vn` is accepted without the
"official source outside the configured Portal" override.

Note for the operator: no Vietnamese traineddata is vendored
(`src/regcompass/languages.py`), so a scanned Vietnamese PDF would be read as
English. Supply text-layer documents, or the Portal's English tree at
`vbpl.vn/en`.
