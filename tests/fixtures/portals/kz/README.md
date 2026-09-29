# Recorded Kazakhstan Portal answers

Captured live on 16 Sep 2026 from `adilet.zan.kz` (the Adilet legal information
system of the Ministry of Justice) under the identified user agent
`RegCompass/0.1.0 (+https://github.com/Ryannurtanio/regcompass)`, one request at
a time, at least 2.0 s apart. **Headless browser: no**, which is the whole
point for this Portal (see below). Five requests in total: robots, landing, the
sitemap twice, and one act page. `/search`, `/advanced-search` and `/api/` were
never requested.

## Files

- `robots_2026-09-16.txt` (6,220 B) - the published rules, and an unusually
  explicit set. Section 1 (`User-agent: *`, which is the group that binds us,
  since RegCompass is not named) allows act pages and forbids `/api/`,
  `/api/internal-export`, `/search`, `/advanced-search`, `/login`, `/register`,
  `/profile`, `/workspace`, `/admin`, `/super-admin` and `/documentation`.
  Section 2 closes the whole site to model-training crawlers by name (GPTBot,
  Google-Extended, CCBot, Bytespider and others; the committed robots.txt has
  the full list);
  section 4 says which crawlers are deliberately left open, namely search and
  citation agents. No crawl-delay is published.
- `sitemap_504_2026-09-16.html` (167 B) - `https://adilet.zan.kz/sitemap.xml`,
  answered HTTP 504 by the gateway on both attempts (11 s each). The sitemap
  its robots.txt points at is not retrievable today.
- `act_Z1300000094_2026-09-16.html` (2,693 B) - the Law on Personal Data and
  Their Protection at `https://adilet.zan.kz/rus/docs/Z1300000094`. It is the
  application shell with the gateway's `<!--SEO_HEAD-->` and `<!--SEO_BODY-->`
  markers left unsubstituted.

## Why Kazakhstan is operator-supplied

The shell carries the Portal's own explanation, in a comment, of what it serves
to clients that do not run JavaScript: a short description of the act, and
deliberately not the text. Verbatim (our translation follows):

> Полного текста здесь нет намеренно: цель - чтобы нас цитировали, а не
> выкачивали корпус одной командой.

"The full text is deliberately not here: the aim is that we are cited, not that
our corpus is pulled in one command."

A headless browser would render around that ask. The Portal's robots.txt also
says in as many words that its published rules are a sign rather than a fence,
and asks polite robots to keep to them. So Discovery is not wired for
Kazakhstan: Documents arrive through "Add document", and the host is
whitelisted so an operator-supplied `adilet.zan.kz` URL needs no override.

Note for the operator: `rus` traineddata is vendored, Kazakh is not, so a
scanned Kazakh PDF would not be read correctly. Prefer the Russian text, which
is also the Portal's default Language in `config/portals.yaml`.
