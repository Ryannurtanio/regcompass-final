# Recorded Indonesian Portal answers

Verbatim response bodies from the Indonesian Portal, `peraturan.bpk.go.id`
(the Audit Board's national regulation database, JDIH BPK), recorded on
**16 September 2026**. They are what the Indonesian tests replay, so no test
makes a request.

## The host, and why it is this one

The planned host was `peraturan.go.id`. That host is unreachable from the machine
this was recorded on: three attempts (45 s, 90 s and 20 s timeouts) all died at
the TCP connect, so nothing at all is known about it from here. It stays on the
Portal whitelist, because a Document a reviewer adds by hand from it must pass
the host check, but Discovery does not point at it.

`peraturan.bpk.go.id` is the national database that answered.

## The requests

**Thirteen requests in this capture, fifteen to this host for the day.** The
other two were the read-only portal research pass earlier the same day, which
asked for `/robots.txt` (200) and `/` (403) under the identified user agent;
request 2 below repeats the second of those. Every request went out at a floor
of 5.0 s from the previous one, and every one is counted here.

| # | rung | URL | status | bytes on the wire |
|---|---|---|---|---|
| 1 | identified | `/robots.txt` | 200 | 177 |
| 2 | identified | `/` | **403** | 5,559 |
| 3 | impersonation | `/` | 200 | 126,172 |
| 4 | identified | `/Search?tentang=perlindungan%20data%20pribadi&jenis=1` | **403** | 5,831 |
| 5 | impersonation | same | 200 | 95,722 |
| 6 | identified | `/Search?keywords=pelindungan%20data%20pribadi&jenis=8` | **403** | 5,831 |
| 7 | impersonation | same | 200 | 239,635 |
| 8 | impersonation | `/Details/229798/uu-no-27-tahun-2022` | 200 | 44,154 |
| 9 | impersonation | `/Search?keywords=informasi%20dan%20transaksi%20elektronik&jenis=8` | 200 | 235,708 |
| 10 | impersonation | `/Search?keywords=perlindungan%20konsumen&jenis=8` | 200 | 239,376 |
| 11 | impersonation | `/Search?keywords=telekomunikasi&jenis=8` | 200 | 232,239 |
| 12 | impersonation | `/Download/224884/UU%20Nomor%2027%20Tahun%202022.pdf` | - | none kept |
| 13 | impersonation | `/Search?keywords=penyelenggaraan%20sistem%20dan%20transaksi%20elektronik&jenis=10` | 200 | 203,354 |

Four requests went out as **identified**, the user agent
`RegCompass/0.1.0 (+https://github.com/Ryannurtanio/regcompass)`; nine used the
**impersonation** rung, the `curl_cffi` Chrome profile, which is the documented
escalation after a refusal and nothing else.

Requests 2, 4 and 6 establish the refusal on two different paths. From request
8 on, the capture went straight to the rung that answers: re-proving a refusal
already proved three times would have spent the Portal's requests to learn
nothing. A Discovery does NOT do that; it asks as itself for every URL and
escalates only on the refusal, which is what `fetch_with_ladder` does and what
`tests/test_portal_id.py` asserts.

Request 12 was a streamed read of the Personal Data Protection Act PDF, capped
at its first 64 KB. It died in the client before a single body byte was read,
so **no PDF bytes were recorded**. Whether the Portal's PDFs carry a text layer
or need OCR is therefore still an open question, recorded in `docs/PORTALS.md`
and left to the operator's supervised Discovery.

## Two edits to the recorded bytes: the secrets the pages carried

Each recorded page carried two credentials of its own, and the **value** of each
has been replaced with `SCRUBBED`. The markup around them is untouched and
nothing else in any file was changed. Neither is read by any test.

- The ASP.NET anti-forgery token, a per-response secret of the form
  `<input name="__RequestVerificationToken" type="hidden" value="...">`. A
  committed secret is a committed secret even when it is scoped to one session.
- The licence key of the PDF viewer the Portal embeds, `licenseKey: '...'`. It
  belongs to the Portal, not to us, and republishing another organisation's key
  in our repository is not ours to do. The repository's own secret scanner
  stops on it, which is the scanner working.

That is why each page is 159 bytes smaller than the wire size in the table
above, except the landing interstitial, which carried neither. On-disk sizes:

| file | bytes on disk |
|---|---|
| `robots_2026-09-16.txt` | 177 |
| `landing_403_identified_2026-09-16.html` | 5,559 |
| `details_uu-no-27-tahun-2022_2026-09-16.html` | 43,995 |
| `search_uu_pelindungan-data-pribadi_2026-09-16.html` | 239,476 |
| `search_uu_informasi-dan-transaksi-elektronik_2026-09-16.html` | 235,549 |
| `search_uu_perlindungan-konsumen_2026-09-16.html` | 239,217 |
| `search_uu_telekomunikasi_2026-09-16.html` | 232,080 |
| `search_pp_penyelenggaraan-sistem-dan-transaksi-elektronik_2026-09-16.html` | 203,195 |

## The files

- `robots_2026-09-16.txt` - the published ask, request 1. It forbids only
  `/Admin/`, `/Identity/`, `/Account/`, `/Manage/`, `/health` and `/Error`, and
  publishes a sitemap. `/Search`, `/Details/` and `/Download/` are all allowed.
  So the Portal's own published rules permit exactly what Discovery does, and
  the 403 comes from the Cloudflare edge's bot fingerprinting rather than from
  any stated rule.
- `landing_403_identified_2026-09-16.html` - the body the identified agent gets:
  a Cloudflare "Just a moment..." interstitial. Recorded during the read-only
  portal research pass earlier the same day, from the same host, under the same
  user agent; it is request 2's answer by shape and size.
- `search_uu_*` and `search_pp_*` - five keyword searches, four restricted to
  `jenis=8` (Undang-undang, the statute type) and one to `jenis=10` (Peraturan
  Pemerintah, the Government Regulation type): requests 7, 9, 10, 11 and 13.
  Each is server-rendered: the result count, the `/Details/<id>/<slug>` links
  and the `/Download/<file id>/<file name>.pdf` links are all in the bytes.
  **These are the provenance of every Indonesian crawl seed**: a test asserts
  that each seeded download reference appears verbatim in one of them, so no
  seed can be invented.
- `details_uu-no-27-tahun-2022_2026-09-16.html` - one law's detail page,
  request 8. It carries the law's metadata, its abstract and its download link,
  but NOT the articles, which is why the Document is the PDF under `/Download/`
  and not this page. It also shows that the download id (224884) is not the
  detail id (229798), so a Document URL cannot be computed from a detail id and
  has to be read off the Portal. `TestOnBytesThePortalActuallyServed` puts this
  file through the Corpus and the Gate, so one test stands on nothing but bytes
  the Portal served.

## What the tests use synthetic bytes for

The recorded files above prove the Portal's shape, its refusal and the
provenance of the seeds. They are not statutes: no statute PDF was recorded
(see request 12). The Discovery and Run tests therefore fetch small
**synthetic** PDFs built inside the test, each a real text-layer PDF carrying a
few lines of Bahasa Indonesia statutory text. They exercise the PDF extraction
lane honestly; they are stand-ins for the Portal's own files, not copies of
them. Each one carries the words "test stand-in, not Portal bytes" in its own
text layer, so anything extracted from one says on its face where it came from.

## The statute links behind the baseline's /Details/ pages (29 September 2026)

`details_downloads_2026-09-29.json` records, for every `/Details/` page the
2025 baseline cites for Indonesia, the statute link that page carries (the
`/Download/` link marked `data-kategori="Peraturan"`) and what the Corpus
extractor reads from that PDF: pages, characters, pages under 50 characters,
and whether the scanned-document rule would send it to OCR.

It is the provenance of the address-seeded Indonesian families in
`config/crawl_seeds.yaml`: `tests/test_official_sources.py` asserts that each
seeded PDF is the statute link of the page the baseline gives for that law.

How it was recorded: 131 requests to `peraturan.bpk.go.id`, all as the
identified user agent (no impersonation was needed that day; every request
answered 200 or an on-host 301), one at a time and at least 5 s apart:
`/robots.txt`, 71 `/Details/` requests (67 pages, 3 on-host redirects
followed, and the Personal Data Protection Law's page asked twice) and 59
statute PDFs. Only the fields above were kept, not the pages or the PDFs.

What it showed:

- Every statute PDF has a text layer carrying the law's number and year on
  its first page. The layers are the Portal's own OCR, so they carry OCR
  slips ("TAHUN 2O2I", "NOMOR TL TAHUN 2OI9" for PP 71/2019).
- Three are mostly page images and would be read by OCR end to end: PP
  5/2021 (739 pages), Permenkominfo 5/2021 (909 pages, 47 MB) and
  Permenkominfo 13/2021 (155 pages). They are not seeded.
- `/Details/198052` (cited for PP 34/2021) now redirects to a Jepara regency
  regulation, and `/Details/98096` (cited for "Ministerial Regulation No.14 on
  Electronic Systems and Transactions 2018") is a Lumajang regency
  regulation. Neither is seeded.
