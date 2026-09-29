# Russian Federation: nothing to record

There are no recorded bytes in this folder because the Portal never answered
over HTTPS. **Headless browser: no.** Nothing was launched.

`publication.pravo.gov.ru` (the official internet portal of legal information)
was attempted three times on 16 Sep 2026 from our network: twice during the
Portal survey (once at a 90 s timeout) and once during these captures
(20 s). Every attempt ended at the TCP connect, with no HTTP exchange at all.
DNS resolves (95.173.157.131), so the name is fine.

**Corrected 22 Sep 2026.** The cause is port 443: our network path blocks it
for every Russian government host tried, while port 80 answers. Over plain
HTTP the Portal exposes an open API (`/api/Documents`, `/api/DocumentTypes`,
no key needed), but what it serves settles the question:

- the acts are image-only scanned PDFs (`/file/pdf?eoNumber=...`), so every
  quote would depend on Russian OCR of a scan;
- it is a gazette of amendments, not consolidated law (for example 149-FZ
  appears only as the acts that amend it);
- its `robots.txt` disallows `/File`, and the working route is `/file/pdf`,
  which is too close to lean on.

So the Thailand fallback was examined and NOT taken, and Thailand stays in the
pool as an upload-only Economy (see `config/portals.yaml`). Nothing was fetched over HTTP beyond the probes, and nothing is
recorded here. Consequences, both recorded in `config/portals.yaml`:

- No host is whitelisted for the Russian Federation. A Document an operator
  supplies therefore needs the "official source outside the configured Portal"
  tick, which discloses on every row from it that a person vouched for the
  source.
- If the Portal is ever wanted, the first step is the same as for every other
  Economy: read `robots.txt`, record it here with its date, and decide the plan
  from what it says.
