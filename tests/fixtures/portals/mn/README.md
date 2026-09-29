# Recorded Mongolia Portal answers

Captured live on 16 Sep 2026 from `legalinfo.mn` (the Legal Information Centre
of the Ministry of Justice and Home Affairs) under the identified user agent
`RegCompass/0.1.0 (+https://github.com/Ryannurtanio/regcompass)`, one request at
a time, at least 2.0 s apart. **Headless browser: no.** Six requests in total
(robots, landing, one category page, the page advertised as an API reference,
one search, and the page script that drives the listing).

## Files

- `robots_404_2026-09-16.html` (3,203 B) - `https://legalinfo.mn/robots.txt`
  answers HTTP 404 with the site's own 404 page. Nothing is published, so our
  2 s floor stands alone. `crawl.parse_robots` reads this as an empty policy,
  which a test asserts.
- `search_personal_data_2026-09-16.html` (93,688 B) - the search results page
  for `sq=хувийн мэдээлэл` ("personal information"),
  `https://legalinfo.mn/mn/search?sq=...`. The query is echoed back in the
  form, the page is headed "Google хайлт" (Google search), and the results
  themselves are left to a client-side widget. Every `legalinfo.mn` link in the
  markup is navigation; not one is a document.

## Why Mongolia is operator-supplied

Three things were checked before concluding:

1. The category page `/mn/law/27` ("Laws of Mongolia") is a shell too: 256 KB
   of furniture with no act in it.
2. `https://legalinfo.mn/api/front/index.html`, which looks from the landing
   page like published API documentation, is an article about the history of
   the Constitution. There is no published API here to build on.
3. The page script `assets/custom/legal/js/pages/law.js` shows where the lists
   actually come from: `$.ajax({ url: URL_LANG + '/ajaxList/', type: 'post',
   dataType: 'json' ... })`, answering with pre-rendered markup in a JSON
   envelope. It is an undocumented POST endpoint, reachable only by guessing
   its filter parameters, and it is not something we will make a Portal
   adapter out of.

So Discovery is not wired for Mongolia. The host is whitelisted in
`config/portals.yaml` so a Document an operator supplies from `legalinfo.mn` is
accepted without the "official source outside the configured Portal" override.

Note for the operator: no Mongolian traineddata is vendored
(`src/regcompass/languages.py`), so a scanned Mongolian PDF would be read as
English and is not usable evidence. Prefer a text-layer document, or the
Portal's English tree at `legalinfo.mn/en`.
