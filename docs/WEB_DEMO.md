# The RegCompass interface

One URL, one process. A reviewer starts a Run, watches it, audits the Mappings
against the source PDF, accepts or rejects them, exports, and switches Engine,
without editing a file or typing a command. The one-page demo that used to live
beside it is retired: its run-start and progress API moved into this
server and its page is gone.

The server is `src/regcompass/server.py` (FastAPI, base-tier dependencies only).
The interface is the React bundle at `src/regcompass/ui_dist/`, built from
`ui/` and committed, so a judge on a pip-only install never needs Node. The
audit view's backend logic is `src/regcompass/audit.py`.

## Run it locally

```
regcompass serve --port 8000
```

Then open http://127.0.0.1:8000 . Equivalent from a checkout, without
activating the environment first:

```
uv run regcompass serve --port 8000
```

There is no module-level application to point a server at: importing
`regcompass.server` builds nothing and opens no database, so a test run or a
stray import beside a live Run cannot touch it.

A Run on Engine A or Engine B needs the live extras (`uv sync --all-extras`), a
running Ollama with `bge-m3` pulled for the Gate, a key (in the shell, in
`.env`, or typed into Settings), and the tesseract binary if an OCR escalation
fires. The fake Engine needs none of that.

Options and environment knobs:

| Option | Variable | Default | Effect |
| --- | --- | --- | --- |
| `--db` | `REGCOMPASS_DB` | `data/regcompass.db` | the working database a Run writes and the audit view reads |
| `--out` | `REGCOMPASS_OUT` | `out` | export directory |
| `--data-dir` | `REGCOMPASS_DATA` | `data` | root for a Corpus Document's stored bytes; must match the data dir Discovery fetched them with |
| `--bundle` | | (unset) | read the audit view from a frozen bundle instead of the working database (see `AUDIT_UI.md`) |

`regcompass demo` and `regcompass audit --bundle X` still work: they print a
one-line note and call `serve`.

## The screens

- **Start a Run** (the Run panel). Economy (every configured Economy, by official name), Pillar
  (all 12, the default pair preselected), Indicator (optional, offered once a
  single Pillar is selected), and the **Engine** control, which offers Engine A
  and Engine B with their declared prices and whether a key is set. The Run
  button posts `{economy, pillars, indicators, engine}` and the progress lines
  stream in below over Server-Sent Events. One job at a time: a second start
  while one is active is refused (HTTP 409). An Economy with no Corpus is also
  a 409, with the message and a Discover button rather than a failure halfway
  through. When the Run finishes, its Mappings open in the audit view scoped to
  that Run.
- **Runs.** Past Run Records newest first: kind, Economy, Pillars, Engine,
  status, start and end, Documents fetched, tokens in and out, and cost in US
  dollars measured from the Engine's declared prices. A row reopens that Run's
  Mappings; the download link hands the record over as a JSON file. The record
  lives in the database, so it survives a restart. The command line reads the
  same table with `regcompass runs` and `regcompass runs show <run_id>`.
- **Evidence.** The Documents of the Run on screen, then the audit view: the
  source PDF with the Verbatim Quote highlighted, the structured Mapping beside
  it, accept / reject / flag, and the Evidence Export. See `AUDIT_UI.md`.
- **Settings.** A password field for the OpenRouter key. The key is held in
  this process for as long as it runs: it is never written to disk, never
  logged and never sent back to the page, and restarting the server forgets it.
  The table shows only "key set" or "not set" per Engine. Below it, **Clear
  downloads and cache**: pick an Economy or **Everything**, press **Preview**
  for the counts, then **Clear now** and confirm on the page. It removes the
  Corpus rows and their bytes under the data root, the stored text streams and
  the Run Records with their Mappings and Review Decisions, for that scope, and
  refuses while a job is running. `regcompass clear --economy XX` and
  `regcompass clear --all --yes` do the same from the command line.

## The API

| Endpoint | What |
| --- | --- |
| `GET /api/status` | job status plus the Economy and Pillar registries |
| `GET /api/engines` | the Engine registry with prices and `key_set`, never a key |
| `GET /api/indicators?pillar=N` | the Indicators of one Pillar |
| `POST /api/run` | start a Run; 409 `corpus_empty` when the Economy has none |
| `POST /api/discover` | fill one Economy's Corpus (the only endpoint that touches the internet) |
| `GET /api/events` | the progress lines, replayed then streamed |
| `GET /api/stats` | live funnel counters, stage times, the last Run Record |
| `GET /api/runs`, `/api/runs/{id}`, `/api/runs/{id}/download` | Run Records |
| `POST /api/settings/key`, `DELETE /api/settings/key/{engine}` | the session key |
| `GET /api/clear/preview?economy=XX` | what a clear of that scope would remove, without removing it |
| `POST /api/clear` | clear the downloads and caches of one Economy or of all; needs `confirm`, refuses while a job is active |
| `GET /api/documents`, `/api/documents/{id}/records`, `/api/documents/{id}/pdf`, `GET /api/records/{id}` | the audit view, optionally `?run_id=` |
| `GET /api/records?sort=confidence&unreviewed=true` | the Review queue: every record of the Run in one list with the Document each came from, lowest Confidence first, plus the counts the screen states (total, below the threshold, unreviewed) |
| `POST /api/reviews`, `GET /api/reviews?run_id=` | record a Review Decision (one per Mapping per Run, a second one replaces the first), and list them |
| `POST /api/reviews/accept-all` | accept every verified Mapping of the Run that carries no decision yet |
| `GET /api/glosses?run_id=` | the English Glosses of one Run, one per Mapping |
| `POST /api/glosses/review` | approve a Gloss with the text and the reviewer's name; a blank name is a 400, because that name is what lets the export drop the AI label |
| `GET /api/export/preview?run_id=` | what the Evidence Export would contain: accepted, rejected, flagged, unreviewed |
| `POST /api/export` | the Evidence Export, CSV and the organizers' filled workbook; on the working database only accepted Mappings enter it |
| `GET /api/db/{table}`, `GET /api/outputs*` | the read-only database browser and the export directory |

The four audit reads take an optional `run_id`; without one the server shows
the newest completed Run. The database browser opens SQLite with the `mode=ro`
URI, so serving a page physically cannot mutate the working database, and only
five tables are reachable.

## Docker

`Dockerfile`, `docker-compose.yml`, `.dockerignore` and `.env.example` package
the app plus a local Ollama server. CPU only.

```
docker compose up --build
open http://localhost:8000
```

That is the whole sequence. There is no manual model pull: the `ollama-init`
service waits for the Ollama server, pulls the Gate embedder `bge-m3` and
exits, and the app waits for it to finish.

- The image installs the tesseract-ocr engine binary via apt and does a
  hash-pinned `pip install --require-hashes -r requirements.txt` (the
  fully-resolved uv export), then `pip install --no-deps -e .` so the
  `regcompass` console script exists inside the container. It runs as the
  non-root user `regcompass` and its CMD is
  `regcompass serve --host 0.0.0.0 --port 8000` against `/data`.
- The image's `HEALTHCHECK` polls `/api/status`, which reads the Engine and
  Economy registries and the in-process Run state only. It answers before any
  Run and with no database present, so a healthy container means the interface
  is serving.
- `docker-compose.yml` runs three services plus two profiles: `ollama`,
  `ollama-init` (the one-shot pull), `regcompass` (the app), then
  `regcompass-bundle` under the `judge-bundle` profile, which serves a frozen
  audit bundle on port 8001 with no key and no Ollama, and `regcompass-demo`
  under the `demo` profile, which seeds a Corpus offline, Runs the fake Engine
  and Exports (`docker compose run --rm --no-deps regcompass-demo`). The demo
  service sets `OPENROUTER_API_KEY` to an explicit empty string, so it is
  keyless even when Compose is reading a developer's `.env`; the app service
  takes the variable as a bare pass-through with no default expression. Compose
  fills a pass-through from the project's own `.env` as well as from the shell,
  which a clean machine does not have and `--env-file /dev/null` reproduces.
- `regcompass seed` is the offline counterpart of `regcompass discover`: the
  legislation bundled under `tests/fixtures/` (which the image carries) enters
  the Corpus through `regcompass.corpus.add_document`, marked source kind
  `fixture`. It is what stops the keyless demo hitting an empty Corpus on a
  clean volume. `src/regcompass/fixtures.py` owns the list. The Document list
  badges those rows and the Evidence Export discloses them in Notes, through
  the same composer the manually added lane uses, so a provision row and its
  Economy's earned zeros carry the same sentence.
- Storage is two named volumes: `ollama-models` for the weights and
  `regcompass-data` mounted at `/data` for the working database, the Corpus
  bytes and the exports. `/app/data` and `/app/out` are symlinks to it, so a
  `docker compose run --rm regcompass regcompass ...` command-line uses the
  same database the server does.
- The headless browser IS installed: Chromium only, through
  `playwright install --with-deps --only-shell chromium`, with
  `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright` so the root-owned download is
  readable by the non-root runtime user. Discovery for a Portal that needs
  Playwright therefore runs in the container, Australia included. The layer is
  662 MB of browser and system libraries, of which roughly 350 MB (334 MiB by
  `du`) is the headless shell itself, second only to the 856 MB dependency
  layer. `--only-shell` leaves out the full browser, several hundred megabytes
  more, which nothing here would start, because every launch in
  `src/regcompass/crawl.py` is `headless=True`. A headful launch
  inside the container would fail for want of that binary.
- The ollama image tag is pinned (`ollama/ollama:0.32.1`); bump it against the
  current release on Docker Hub if needed.

## How the app reaches Ollama

Both call sites resolve the endpoint from an environment variable with a local
default, so nothing is hardcoded in a way that breaks under Docker:

- `src/regcompass/map.py` (the mapper lane):
  `base = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")`
- `src/regcompass/gate.py` (the bge-m3 embedding Gate): the same pattern.

`docker-compose.yml` sets `OLLAMA_HOST=http://ollama:11434` for the app
container; a native local run can leave it unset. `config/models.yaml` names
the shared embedder `ollama/bge-m3`, and LiteLLM combines that name with the
`OLLAMA_HOST` base URL, so there is nothing to override in config.

## Honest limitations

- One job at a time by design. The state is a single in-process manager and
  concurrent starts are refused with a 409. This is an interface, not a queue.
- A Run needs Ollama up with `bge-m3` pulled (the Gate embeds locally whichever
  Engine answers). A dead Ollama surfaces as a clean error line in the progress
  log, not a crash.
- The database browser reads one configured database (read-only) and the
  outputs listing one configured directory; neither accepts a path from the
  browser (traversal on output file names is rejected).
- Server-Sent Events stream over a polling generator suited to a single client.
  It is intentionally simple, not a high-fanout broadcast bus.
- The session key is per process. Two workers behind a load balancer would not
  share it; the server is meant to run as one process.
