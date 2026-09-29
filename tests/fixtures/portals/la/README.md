# Recorded Lao Official Gazette answers

Ten requests to `laoofficialgazette.gov.la` on 16 Sep 2026, nine between 01:47
and 01:51 UTC and one at 02:17, all under the identified user agent
`RegCompass/0.1.0 (+https://github.com/Ryannurtanio/regcompass)`, on one
connection, spaced at least 2.0 s apart, robots.txt first. Every body here is
the verbatim response; only the PDF was cut short, deliberately (see below).
One further connection attempt, the first try at the tenth request, was reset by
the host during the TLS handshake, so no HTTP request reached it; the retry two
minutes later answered 200. No other request was made to this host.

| File | URL | Status, bytes |
|---|---|---|
| `robots_2026-09-16.html` | `/robots.txt` | 200, 135,911 |
| `listing_electronic_2026-09-16.html` | `/index.php?r=site/index&Document%5Btitle%5D=%E0%BB%80%E0%BA%AD%E0%BB%80%E0%BA%A5%E0%BA%B1%E0%BA%81` | 200, 138,875 |
| `listing_electronic_page2_2026-09-16.html` | the same, plus `&Document_page=2` | 200, 135,826 |
| `listing_cyber_2026-09-16.html` | `...&Document%5Btitle%5D=%E0%BB%84%E0%BA%8A%E0%BB%80%E0%BA%9A%E0%BA%B5` | 200, 129,973 |
| `listing_consumer_2026-09-16.html` | `...&Document%5Btitle%5D=%E0%BA%9C%E0%BA%B9%E0%BB%89%E0%BA%8A%E0%BA%BB%E0%BA%A1%E0%BB%83%E0%BA%8A%E0%BB%89` | 200, 134,163 |
| `listing_telecom_2026-09-16.html` | `...&Document%5Btitle%5D=%E0%BB%82%E0%BA%97%E0%BA%A5%E0%BA%B0%E0%BA%84%E0%BA%BB%E0%BA%A1` | 200, 138,692 |
| `listing_telecom_page2_2026-09-16.html` | the same, plus `&Document_page=2` | 200, 131,835 |
| `listing_computer_2026-09-16.html` | `...&Document%5Btitle%5D=%E0%BA%84%E0%BA%AD%E0%BA%A1%E0%BA%9E%E0%BA%B4%E0%BA%A7%E0%BB%80%E0%BA%95%E0%BA%B5` | 200, 133,449 |
| `law_display_2023_2026-09-16.html` | `/index.php?r=site/display&id=2023` | 200, 91,890 |
| `electronic_transactions_2022_head_64k.pdf` | `/kcfinder/upload/files/31%E0%BA%AA%E0%BA%9E%E0%BA%8A2022.pdf` | 200, first 65,536 bytes only |

The five filtered listing URLs are the Lao seed words of
`config/crawl_seeds.yaml`: ເອເລັກ (elec-), ໄຊເບີ (cyber), ຜູ້ຊົມໃຊ້ (consumer),
ໂທລະຄົມ (telecom) and ຄອມພິວເຕີ (computer). The last two words are both in the
cybersecurity family because the Portal titles the 2025 Law on Cyber Security
with ໄຊເບີ and the cyber crime law with ອາຊະຍາກຳທາງລະບົບຄອມພິວເຕີ, so one word
reaches only one of them.

## What these bytes show

**robots.txt is a soft 404.** The host answers `/robots.txt` with HTTP 200 and
135,911 bytes of its own homepage. That body publishes no rules, and reading
rules out of markup would invent both permissions and refusals, so `parse_robots`
recognises an HTML body and returns the empty policy. Nothing is disallowed and
nothing is permitted by it: our own 2.0 s floor is the whole promise.

**The legislation grid is the search.** `/index.php?r=site/index` renders a Yii
grid (`id="homelegal-grid"`) whose `Document[title]` filter matches anywhere in
an instrument's title and whose `Document_page` walks the results ten to a page.
The filters returned 17, 1, 5, 11 and 4 matching instruments on the day.

**The page carries a second, look-alike table.** Below the grid sits an
unfiltered table of recent publications with the same row markup. Everything the
adapter reads is scoped to the grid element, and the summary line inside it
("ສະແດງ 1-10 ຂອງ 17") is what says whether another page exists: when the results
fit one page the Portal renders no pager at all.

**Two PDF columns, read by their headers.** The grid's last two columns are
`PDF ອັງກິດ` (English) and `PDF ລາວ` (Lao). The Lao file is the official text and
the only quote source; where an English rendering exists its URL is recorded on
the Document's notes. The Consumer Protection law is the one recorded instrument
published in both, and its two files are named the other way round
(`Law on Consumer Protection.pdf` sits in the ENGLISH column,
`Protecting consumers Law .pdf` in the Lao one), which is why the columns are
read by header rather than by filename.

**The instrument's own page adds nothing.** `site/display&id=2023` is the Law on
Electronic Transactions (Amended). Its page carries exactly the one PDF link its
grid row already gave, so Discovery reads the listing and stops instead of
spending one request per instrument.

**The PDF was cut at 64 KB on purpose.** It is here as evidence that the file
URL answers 200 with `application/pdf` to the identified agent, not as a
Document: a truncated PDF is not a statute. The canonical 29-page Lao scan and
its two-page slice already live under `tests/fixtures/sample_legislation` and
`tests/fixtures/derived`, and `tests/test_non_english_lane.py` runs the OCR and
quote-verification path on them.

`tests/test_lao_portal.py` replays these bytes; no test in the repository
requests anything from this host.
