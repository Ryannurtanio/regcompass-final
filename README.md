# RegCompass - AI Tool for Digital Trade Regulatory Analysis

UN Global Hackathon on AI for Digital Trade Regulatory Analysis
Team: RegCompass | Round: **Final**
Last updated: 2026-09-28

> **Final round requirement.** Every section below is mandatory, as flagged in the Round 1 version of this
> template. This README is part of your 30 September submission and is read during the desk review - it is
> where a reviewer looks first, and it is the front door to criterion **C4a, Technical Handover (8 points)**.

RegCompass maps national legislation to the UN ESCAP RDTII 2.1 framework and proves every
row it ships. The Engine may only SELECT a passage of the source text; a mechanical
byte-for-byte substring check against the canonical extraction stream rejects anything else.
Unverifiable output is dropped and logged, never paraphrased into place.

---

## What This Tool Does

This tool automates two tasks required by the ESCAP Regional Digital Trade Integration Index
(RDTII 2.1):

**Task 1 - Automated Evidence Discovery**
Given an Economy and a Pillar, Discovery reads the official government Portal, fetches the
relevant legislation (including scanned and image-based PDFs) into that Economy's Corpus, and
extracts clean, structured text, with no manual steps. Discovery is the only step that touches
the internet.

**Task 2 - Intelligent Mapping and Categorisation**
A Run reads the Corpus and maps its text to RDTII Indicator IDs. Each provision is recorded as
a Mapping with an article-level citation, a Verbatim Quote checked byte for byte against the
source, and a Discovery Tag marking whether it was found independently (NEW) or matched a known
example (KNOWN).

**Mandatory pillars:** 6 (Cross-border data policies) and 7 (Domestic data protection and
privacy).
**Also in scope:** all twelve RDTII 2.1 pillars. All 62 Indicators are generated from the
organizer's own sheets, so a Run on any Pillar produces Mappings. The RDTII 2.1 scoring rubric
exists only for nine of the ten Pillar 6 and 7 Indicators (6.5 has no rubric and is not scored),
so every other Indicator takes the mappings-only path: its Mappings export in full and its score
cell is left blank rather than guessed.

**Economies covered:** Australia, Malaysia, Singapore, Indonesia, Thailand, Lao PDR, Viet Nam,
China, India, Kazakhstan, Mongolia, Russian Federation. Every one of them is configured in
`config/portals.yaml`; adding another is an edit to that file, with no code change. Six of them
were pre-run on both declared Engines for Pillars 6 and 7, from a Corpus fetched from the
official source: **Australia, Malaysia, Singapore, China, Indonesia and India** (see **Pre-run
Coverage**).

**Ready for the live test.** Any of the nine Economies whose 2025 RDTII database we hold can be
mapped today by adding its Documents in the interface (by Portal address or upload); what differs
is how far automated Discovery has been proven. The honest state on 28 September 2026:

- **Run end to end on both Engines, Pillars 6 and 7, from a Corpus fetched from the official
  source:** Indonesia (7 Documents, live Discovery on 23 September 2026), India (5 Documents:
  4 by live Discovery on 23 September 2026 and the Information Technology Act, 2000 uploaded
  with its official India Code Source URL) and China (3 statutes added by their Source URLs on the
  regulator's site). Their figures are in **Pre-run Coverage**.
- **Corpus fetched by live Discovery, not yet Run:** Lao PDR (37 Documents from the Official
  Gazette on 23 September 2026). It is ready for a Run; we did not spend on pre-running it.
- **No Discovery, add by URL or upload:** Thailand, Viet Nam, Kazakhstan, Mongolia, Russian
  Federation. India's Portal cannot serve its own `robots.txt`, and it discovers under a
  configured policy that says what that silence means for it (see **Crawling Politely**).

Every one of the nine can be mapped today by adding its Documents through the interface. Only
Discovery differs.

**Starting empty.** The step before the clock starts is on the **Settings** screen, section
**Clear downloads and cache**: pick **Everything** (or one Economy), press **Preview** to see
what would go, then **Clear now**. The Corpus and the **Run history** screen are empty
afterwards, and the first Run of the live test fetches and extracts from nothing.

---

## Quick Start

⚠ **A competent programmer must reach a working system from this section alone, on a clean
machine, in under 30 minutes - with no help from your team.**

The supported path is Docker. It needs nothing on the machine but Docker itself: no Python, no
Node, no OCR install, no model download step. If you have no Docker, or it will not start, go
straight to **6. Without Docker** at the end of this section: it is a complete path of its own
and it reaches the same interface.

### 1. Clone the repository

    git clone https://github.com/Ryannurtanio/regcompass-final.git regcompass
    cd regcompass

### 2. Set up the environment

Docker, with the `compose` plugin, is the only prerequisite. Nothing is installed on the host:
Python, the OCR engine, all six vendored language files, the built interface, the sample
legislation a Corpus can be seeded from and the frozen evidence bundle are all inside the image,
and the embedder is pulled automatically at first start. There is no separate install step, so
go on to step 4.

The headless browser is in the image too, so Discovery for a Portal that needs one, Australia's
above all, runs inside the container exactly as it does on the host. The browser and its system
libraries are 662 MB of the image size quoted in step 4, second only to the Python dependencies
at 856 MB. On the host path the same browser is one extra command, listed under "Without
Docker" below.

**Docker not running, or not installed?** The whole host path is its own section, **6. Without
Docker**, at the end of this Quick Start. It replaces steps 2, 4 and 5; steps 1 and 3 are the
same either way. Compose does for you what that section asks you to install, which is why
Docker is the supported path.

### 3. Configure

No configuration is required to start. A key is only needed to run a declared Engine.

Two ways to supply one, neither of which writes it to disk:

- Type it into the interface: **Settings** screen, field **Key**, button **Save for this
  session**. It is held in the server's memory only and forgotten when the server restarts.
- Or export it before the command: `export OPENROUTER_API_KEY=...` and `docker compose up`
  passes it through.

Both declared Engines read the same variable, `OPENROUTER_API_KEY`. See **Your Two Declared
Engines**.

### 4. Start the interface

    docker compose up

One command. It builds the image, starts a local Ollama server, pulls the Gate embedder
(`bge-m3`, about 1.2 GB), waits for it, and serves the interface on port 8000. There is no
manual pull step to follow.

These timings are a record of one measured start, not a figure any test asserts. Measured on
16 September 2026 on a clean macOS laptop, linux/arm64, fresh volumes, single run: Ollama
running at 175 s, embedder pulled at 192 s, the app answering `/api/status` at **201 s**. The
image was 1.7 GB then. Carrying the headless browser it is **2.6 GB**, measured on the same
machine on 23 September 2026; a build with the dependency layer already cached took 84 s.

Then open **http://localhost:8000**. **Everything else happens in the interface**: starting a
Run, reviewing, correcting, switching Engines, exporting. A reviewer does not need the command
line again after this step.

To host a copy on a rented server behind a login and HTTPS, see
[docs/DEPLOY_VPS.md](docs/DEPLOY_VPS.md). There the app opens on its own sign-in page, and
the header gains a "Log out" button; the local path above has no login at all.

### Getting the prepared database

The pre-run Corpus and Runs (see **Pre-run Coverage**) ship as one archive attached to the
GitHub Release, not inside the repository or the image. The loader checks its SHA-256
before unpacking and never replaces a database that already holds Runs unless forced.
On the Docker path, load it into the volume with the app stopped:

```bash
docker compose stop regcompass                      # only if the stack is already up
docker compose run --rm --no-deps regcompass-load   # download, verify, unpack
docker compose up                                   # then open http://localhost:8000
```

A successful load prints `SHA-256 verified (401 MB)`, one line per Economy (AU 18 Documents,
CN 3, ID 7, IN 4, MY 21, SG 10, each with 4 Runs), and ends `loaded 63 Documents and 24 Runs`
(63 source files written). The download time depends on your connection; the check and the
unpack after it take seconds.

On the host path the same step is `uv run regcompass load-data`, listed in **6. Without
Docker**. Details, sizes and the form that names the address and SHA-256 yourself
(`--url`, `--sha256`): [docs/RELEASE_DATA.md](docs/RELEASE_DATA.md).

What you get once it is loaded:

- **24 Runs, listed with 6 Discovery records.** The **Run history** screen lists every record, so
  it shows 30 rows: the 24 Runs (six Economies, two Engines, Pillars 6 and 7), each marked
  **Run** in the Kind column, and the 6 Discoveries that fetched their Corpus, marked
  **Discovery**.
- **No Review Decisions.** Accepting or rejecting a Mapping is the reviewer's act, so the
  archive carries none, and every prepared Run's Evidence Export is empty until you accept
  rows. On the **Evidence** screen, the button **Accept all N…** (N is the count not reviewed
  yet) asks *Accept all N Mappings not reviewed yet?*, and **Accept N Mappings** then accepts
  every undecided Mapping of that Run; rows already accepted, rejected, flagged or corrected stay
  as they are. Review row by row instead if you want to judge them first.
- **63 Documents, not 64.** The archive leaves out one Document the Runs read: India's Hindi
  rendering of the Telecommunications Act, 2023 (`doc_in_H202344`). Its PDF's text layer
  lost every Devanagari glyph, so its text is not the law's text and it is not shipped. India
  arrives with 4 Documents, and its Runs' Mappings from that rendering are not in the
  archive. **Pre-run Coverage** still reports what was run: 5 India Documents, 64 in all.

### 5. Verify

Two ways, depending on whether you have a key.

**With no key at all** (no network, no Ollama, no model): the keyless demo seeds a Corpus from
the legislation bundled in the image, Runs the offline fake Engine over it, and writes the
Export.

    docker compose run --rm --no-deps regcompass-demo

Expected, in a minute or two: Australia seeded with one Document (`doc_au_C2026C00098VOL01`,
666 pages), a Run on Pillar 7 that ends `AU: 1 documents, 100 gated pairs -> 60 verified,
20 no-evidence, 20 dropped, 3 groups reconciled`, and an Export that reports
`battery GREEN: 62 rows` and writes `submission.xlsx`, `submission.csv` and the JSON files to
`/data/out`. Most of the time goes on reading the 666-page Act once; the Run and the Export take
seconds. It writes to the same volume the interface reads, so the Run is in the **Run
history** list when you open http://localhost:8000. Any bundled Economy works the same way:

    docker compose run --rm --no-deps regcompass-demo sh -c \
      'regcompass seed --economy SG && regcompass run --engine fake --economy SG --pillar 7 && regcompass export'

**With a key:** a Run reads a Corpus and never fetches, so it needs one first. The image itself
carries no working database (`data/` is excluded from the image by `.dockerignore`), so a fresh
volume has no Corpus until you either load the prepared database (**Getting the prepared
database** above), add a Singapore Document in the interface, run Discovery for Singapore, or
seed the bundled Singapore legislation with `regcompass seed --economy SG`.

Then open the interface, go to **Start a Run**, choose Economy **Singapore**, Pillar **7**, Engine
**Engine A: GPT-5.6 Luna** or **Engine B: Qwen3-30B-A3B-Instruct-2507**, press **Start Run**, and
confirm with **Yes, start the Run** once you have read the cost estimate. Progress appears in the
Run view below the panel, Document by Document and Step by Step, and stays there after the Run
finishes. Then press **Open Evidence** (or open the **Evidence** screen), click a Document, accept
the rows you agree with, press **Export workbook**, and take the file with **Download workbook** in
the footer.

**Known first-run symptoms.**

- *"No Documents yet for &lt;Economy&gt;"*: that Economy's Corpus is empty, and **Start a Run** says
  so before you press anything. Fill it one of three ways, all named on that screen: press
  **Discover** to collect from the Portal, open **Add document** to upload a file or give an
  official URL, or run `regcompass seed --economy <code>` for a bundled Economy.
- *Adding a file you saved by hand*: **Source URL** is optional on the upload lane, so the
  Document enters the Corpus and Runs immediately. Its rows then open **Open source (local
  copy)**, and the Evidence Export refuses them by name until you record the official address.
  The Corpus list under **Add document** marks such a Document **no Source URL** and offers
  **Set Source URL** on its row: type the address, press **Save**, and the same export ships.
  Use that control rather than adding the Document again by URL, which would fetch the file a
  second time and leave a duplicate.
- *Several laws published on one page*: give every one of them that same **Source URL**. Several
  Documents may share an address, each keeping its own file, its own text and its own name, and
  every row from each carries that address. Give each file a name of its own, though: the
  Document is identified by its file name, so two different laws both saved as `act.pdf` are
  refused with a message asking you to rename one rather than quietly becoming one Document.
  Uploading the same file twice under the same address is not an error at all; it hands back the
  Document you already have and changes nothing.
- *"nothing exported until you accept"*: only accepted (or corrected) Mappings enter an Evidence Export. Use
  **Accept all N…** on the Evidence screen, then confirm, to ship everything undecided.
- *Running compose from a checkout that has its own `.env`*: Compose reads that file and the key
  in it reaches the container even when your shell has none. Add `--env-file /dev/null` to
  reproduce a clean machine exactly.

### 6. Without Docker

This section **replaces steps 2, 4 and 5**, it does not follow them. Steps 1 (clone) and 3
(configure) are the same on either path. Everything you need is here; you should not have to
read the Docker steps at all.

**Prerequisites**, three of them. Ollama is needed only to **start a new Run**: the Gate
embeds passages during a Run and at no other time. Browsing the prepared Runs, the audit
view, the Comparison and the Evidence Export read the database and never call Ollama, so a
reader who only reviews the prepared data can skip Ollama and its 1.2 GB pull.

| What | Why it is needed | How to get it |
| :---- | :---- | :---- |
| [`uv`](https://docs.astral.sh/uv/) | Installs the dependencies and runs every command. It also fetches Python 3.12 itself, from `.python-version`, so a machine with no Python is fine. | `curl -LsSf https://astral.sh/uv/install.sh \| sh`, or `brew install uv` |
| tesseract | The OCR engine, for scanned laws. A born-digital PDF never reaches it. | `brew install tesseract`, or `apt-get install -y tesseract-ocr tesseract-ocr-eng` |
| [Ollama](https://ollama.com) with `bge-m3` | The Gate embeds every candidate passage locally, whichever Engine answers, and refuses with a clear error when it cannot reach the server (`src/regcompass/gate.py:176`). | Install Ollama, then the two commands below |

The commands, in order, from the directory you cloned into:

    ollama serve                      # leave it running in its own terminal (only to start a new Run)
    ollama pull bge-m3                # the Gate embedder, about 1.2 GB (only to start a new Run)
    uv sync --extra live              # dependencies, and Python 3.12 if needed
    uv run playwright install --only-shell chromium   # the headless browser, for Australia's Portal (on Linux add --with-deps)
    uv run regcompass load-data       # the prepared database: download (about 400 MB), verify, unpack into data/
    uv run regcompass serve           # http://127.0.0.1:8000

`load-data` takes the archive's address and SHA-256 from `config/prepared_data.yaml`; to name
them yourself, pass `--url` and `--sha256` (both are on the Release page, see
[docs/RELEASE_DATA.md](docs/RELEASE_DATA.md)). Run it before `serve`, or stop `serve` first
with Ctrl+C. It refuses a database that already holds Runs unless you add `--force`. What the
prepared data holds, and the one Document it leaves out, is under **Getting the prepared
database** above. Skip it to start from an empty Corpus.

Then open **http://127.0.0.1:8000**. As on the Docker path, everything else happens in the
interface: starting a Run, reviewing, correcting, switching Engines, exporting.

**Choosing a port.** `serve` takes `--port`, and binds to `127.0.0.1` by default, so the
interface is reachable from that machine and not from the network:

    uv run regcompass serve --port 8100

Two people sharing one machine must each pick a different port, otherwise the second `serve`
exits with an address-already-in-use error. The port is the only thing they have to agree on;
each also wants their own `--db` and `--data-dir` if they do not want to share a Corpus.

**Verify, with no key and no Ollama.** The fake Engine is offline end to end, at the Gate as
well as at the model (`src/regcompass/engines.py:485`, `embed_fn_for`), so this works before the embedder has
finished downloading. Stop the server, or open a second terminal, and run:

    uv run regcompass seed --economy AU
    uv run regcompass run --engine fake --economy AU --pillar 7
    uv run regcompass export

Expected: Australia seeded from the legislation in the repository, a Run on Pillar 7 (60
verified, 20 no-evidence, 20 dropped, 3 groups reconciled), and an Export of 62 rows
(`battery GREEN: 62 rows`) written to `out/`, in a minute or two on a laptop (the seeded Act is 666 pages, and the
fake Engine reads all of it). This is the same three commands the Docker
demo service runs, against the same default working database `data/regcompass.db`, so start
`serve` again afterwards and the Run is in the **Run history** list and the **Evidence** screen. Open
it, click a Document, and you are looking at a Verbatim Quote highlighted on the page it came
from: the tool works. Any bundled Economy does the same with `--economy MY` or `--economy SG`.

Neither `data/` nor `out/` is in the repository, because both are gitignored. You do not have
to create them: the first write makes the directory it needs.

**How long this takes.** Two one-time downloads dominate a clean machine, and both depend
entirely on your connection: `ollama pull bge-m3` (about 1.2 GB) and the first `uv sync`, which
builds the environment from scratch. Everything after them is cached, so a second clone on the
same machine is quick. No figure here is asserted by a test. If you are in a hurry, start the
pull, and run the keyless verify above while it downloads.

---

## Your Interface

Criteria **C3a (10)** and **C3b (5)** are marked on your interface during the desk review, by someone who did
not build it. Describe how to reach each of the following, with the screen name and the control:

| What a reviewer needs to do | Where it is |
| :---- | :---- |
| Start a run and watch progress in plain words | **Start a Run** screen. The **Run panel** box on it takes four steps, **Economy**, **Pillar**, **Indicators** and **Engine**, and **Your Run** beside them sums up the choice with what the last Run on that setup took and cost. Press **Start Run**, read the estimate, then **Yes, start the Run**. The Run view appears below the panel and follows every Document Step by Step: **Read**, **Scan check**, **Split into sections**, **Gate**, **Map and Prove** and **Gloss** (in the code: M1 extract, M2 OCR, M4 chunk, M5 gate, M6 map + M7 verify, M3 gloss), then **Reconcile** once for the whole Run (M8). A Step with nothing to do is shown as skipped rather than going missing. **What the Run has found so far** counts Documents, Sections, Candidates kept by the Gate, Mappings proposed and Mappings proven; **Where the time went** charts each Document's time, Step by Step when you point at it; the header shows the time and what the Run spent at the Engine's declared prices (**Cost so far (our meter)** while it runs, then **Cost (our meter)** and **Engine calls**, with the provider's bill beside it). Click a Document's name for its own drill-down: its **Steps**, its Mappings by Indicator, the **Candidates the Gate kept**, and each quote marked in its source; **Back to the Run** (or `Esc`) returns. **Show raw log** opens the plain log with the code's stage names. The Run view stays on the screen after the Run finishes. |
| Open the audit view: a result beside the source text it came from | **Evidence** screen, click any row of the **Documents** table (or press Enter). The source PDF opens on the left, the Mapping on the right, with the quote highlighted on the page image. Scanned laws render like any other: the image decoders ship inside the interface, so this works with no internet. A source that is a web page rather than a PDF (China's three statutes) shows its text on the left instead, with the Verbatim Quote marked where it sits and no page number, because a web page has none. The header names the Run on screen (id, Economy, Pillar, Engine), and the record pane prints the **Confidence** composite as a number beside its dots. **How this is scored**, beside the dots, opens the four signals it is computed from (**Meaning match** 45%, **Quote length** 25%, **Specificity** 15%, **Proof attempts** 15%), each with its raw value, weight and contribution and a line on why it counts, adding up to the Confidence shown. The Engine writes the Rationale; the pipeline computes the Confidence. |
| Add a Document to a Corpus by hand | **Start a Run** screen, control **Add document** (also the link beside **Corpus of &lt;Economy&gt;** under **Your Run**): an optional **Law name** (the statute's own name, which the Evidence Export writes into the **Law Name** column), an optional **Source URL** on the upload lane, the file or the URL to fetch, and the **Language**. The control lists that Economy's whole **Corpus** underneath, so an upload is visible the moment it lands, before any Run. Several Documents may share one **Source URL**, which is what a ministry landing page publishing a whole collection needs. |
| Record where an added Document is published | **Start a Run** screen, **Add document**, the **Corpus** list: a Document with no address is marked **no Source URL** and carries a **Set Source URL** field on its row. It edits the Document already in the Corpus, so nothing is fetched and no duplicate is created. |
| See why nothing happens on a fresh install | **Start a Run** screen. An Economy with no Documents says so above the **Run panel** and names the three ways to fill it: button **Discover**, control **Add document**, or `regcompass seed --economy <code>` for the keyless demo. |
| Follow a row to its official source at the cited article | **Evidence** screen, audit view, link **Open source, page N** on the citation line *section subsection, PDF page N*. It opens the Document's official **Source URL** in a new tab, at the cited page (`#page=N`). A web-page source carries no page number on either. The same link sits on every row of the **Comparison** screen. A Document with no Portal address recorded reads **Open source (local copy)** and opens the stored file the highlight was drawn on. |
| Accept, reject or correct a row | **Evidence** screen, a **Documents** row, then buttons **Accept**, **Reject**, **Flag** (keys `A`, `R`, `F`) with an optional **Note**. To file a Mapping under a different Indicator: button **Correct** (key `C`), then **Correct to** (an Indicator of the Run's own Pillars), a required **Reason** of 1 to 300 characters, an optional **Your name**, and **Save correction**. The Engine's Mapping is never modified: the record reads *Engine proposed X (title), reviewer corrected to Y (title)*, and every decision on a Mapping is kept in an append-only history (`GET /api/reviews/history`). To correct an English rendering: field **English gloss**, then your name in the field beside it and **Mark reviewed**. |
| Work through the rows that need a person first | **Evidence** screen, view **Review queue**: every record of the Run in one list, lowest **Confidence** first, whichever Document it sits in, with a count line (*14 of 60 below 0.60 Confidence, 9 not reviewed yet.*) and two filters, **Not reviewed only** and **Corrected only**. A row opens the same audit view, and `A`, `R`, `F`, `J` and `K` then step in queue order (`C` opens the Correct picker and stays on the row). A row the pipeline could not score reads **not scored**: it sorts first and counts as below the threshold, because nothing is known about it. |
| Switch the AI engine | **Start a Run** screen, **Run panel** step **4 Engine**: one card per Engine. It opens on the Engine `config/models.yaml` declares as the default (**Engine B**, the open-weight one, marked *The registry default.*), so starting a Run never spends on the commercial Engine by accident. Each card shows the Engine's per-token price and **Key set** or **No key: add one in Settings**. |
| Export to the RDTII schema | **Evidence** screen (or the audit view), header button **Export workbook**. The footer then offers link **Download workbook** (the organizer's `submission.xlsx`) with the CSV and the two JSON files beside it, so the export lands in your own downloads folder and not only in the server's volume. |
| Produce the Engine Comparison file | **Comparison** screen: it opens on the Economy and Pillar of your most recent completed Run. Choose Run A and Run B (the same Economy and Pillar on the two Engines), then the links above the table: **Download sheet (xlsx)**, **Sheet (CSV)**, **Rows (CSV)** and **JSON**. |
| Check how politely the crawler behaves | **Settings** screen, card **Polite crawling** (read-only, from `GET /api/settings/politeness`): each Portal's sites and minimum wait between requests, 1 connection at a time per host, a **robots.txt respected** switch that is locked on, and what happens when a Portal's robots.txt cannot be read. |
| Clear downloads and cache before the clock starts | **Settings** screen, section **Clear downloads and cache**: pick an Economy or Everything, **Preview**, then **Clear now**. |
| See why a Run that finished has nothing to show | **Evidence** screen, the banner above the lists. A Document whose text carries no section structure becomes one unstructured chunk, the Gate reads section chunks only, and so no Engine is ever asked about it. The banner names the Document and says to check the file rather than the Engine; the Run view says so on that Document's row (*No numbered headings found, so the whole text was one section and the Gate kept nothing.*), the same sentence is in the raw log at *M4 chunk*, on the Run Record, and in the **Export workbook** refusal. |
| Tell a Run that died from one still going | **Run history** screen, column **Status**. A Run whose process ended without closing its record reads **Interrupted**, set when the server next starts, because the one worker thread lives in the process that has just begun. Nothing is left reading *Running* forever, and a new Run is never refused by a Run that is not there. |

The five screens are **Start a Run**, **Run history**, **Evidence**, **Comparison** and
**Settings**. The **Run panel** is the setup box on Start a Run, not a screen of its own. The
Evidence screen lists a Run two ways, **Documents** and **Review queue**. The audit view is
reached from either and left with **← Documents** or **← Review queue**, whichever it was opened
from.

**Watch a Run again.** On **Run history**, a Run whose events were recorded carries the link
**Watch again**. It opens **Watch a Run again**: the same Run view, played back from the recorded
events at a chosen **Speed**, with **Watch from the start**, **Pause** and **Resume**. Nothing runs
again and no Engine is called. **← Run history** goes back. Each row also carries **Compare**
(the Comparison screen on that Economy and Pillar) and **Record** (the Run Record as a file).

**Walkthrough recording:** three to four minutes, submitted with the Word document and linked
from the GitHub Release page of the tag in **Release** below.

---

## Your Two Declared Engines

| | Engine A - commercial hosted | Engine B - open weights |
| :---- | :---- | :---- |
| Provider and model | OpenAI GPT-5.6 Luna, served through OpenRouter | Alibaba Qwen3-30B-A3B-Instruct-2507, served through OpenRouter |
| Version / checkpoint | `openai/gpt-5.6-luna`, pinned, temperature 0, transport retries 0 | `qwen/qwen3-30b-a3b-instruct-2507`, pinned, temperature 0, transport retries 0 |
| Local or hosted API | Hosted API. No local form exists. | Hosted API by default. Open weights, so the same checkpoint also runs locally (see below). |
| Config value | registry key `engine-a` in `config/models.yaml`: `provider: openrouter`, `litellm_model: openrouter/openai/gpt-5.6-luna`, `api_key_env: OPENROUTER_API_KEY` | registry key `engine-b` in `config/models.yaml`: `provider: openrouter`, `litellm_model: openrouter/qwen/qwen3-30b-a3b-instruct-2507`, `api_key_env: OPENROUTER_API_KEY` |

The template's `LLM_PROVIDER` and `LLM_MODEL` are, in this repository, the `provider:` and
`litellm_model:` fields of one registry entry in `config/models.yaml`; an Engine is selected by
its registry name (`engine-a`, `engine-b`), never by a loose pair of environment variables, so
the two halves can never drift apart.

Both Engines cleared the shipped accuracy gate. On the 11-pair evaluation set, ten passes each
at temperature 0 against a bar of 0.90 per pass: Engine A passed 10 of 10, mean 0.973, lowest
0.91; Engine B passed 10 of 10, mean 0.945, lowest 0.91. The gate is a test in this repository,
not a scratch harness: it writes its score, pair count, tokens and cost to `tests/.paid/` on the
machine that runs it (that folder is gitignored), so the numbers above are regenerated by
rerunning the paid gate, not asserted.

The honest margin: the lowest pass on either Engine is 0.91 against a bar of 0.90, and the pairs
that flip are Malaysia sections 6 and 9 against Indicator 6.4, both of which the ESCAP baseline
cites directly and which the evaluation set therefore expects to map. Both Engines clear the bar
on every pass; neither clears it comfortably on those two pairs.

**How many calls a Run keeps in flight.** Each registry entry in `config/models.yaml` carries a
`concurrency` field: 4 on a hosted Engine, because the sealed live test does not fit the window
one Mapping call at a time, and 1 on the fake Engine (an Engine repointed at a local checkpoint should carry 1 too), which is
one machine and gains nothing from a queue. `--concurrency` on `regcompass run` and on
`regcompass e2e` overrides it for one Run, between 1 and the ceiling of 32. The value changes
only how long a Run takes: records are assembled in pair order whatever it is, so the exported
workbook is byte-identical at any value, and the Run Record stores the value actually used
beside the number of times a provider asked the Run to slow down.

### Switching between them

In the interface: **Start a Run** → **Run panel** step **4 Engine** → pick the Engine's card.
Nothing is edited and nothing is typed at a command line. The two options are exactly **Engine A:
GPT-5.6 Luna** and **Engine B: Qwen3-30B-A3B-Instruct-2507**; each card says **Key set**, or
**No key: add one in Settings** when it is not.

The abstraction lives in `src/regcompass/engines.py`, which is the single resolution point for
every model-calling stage. Adding a provider is one entry in `config/models.yaml` with its
`litellm_model` identifier and the name of the environment variable holding its key; because
every call routes through litellm there is no per-provider plugin to write. `regcompass engines`
lists the registry and says whether each key variable is set, never its value.

**Running Engine B locally.** Engine B is open weights, so the same checkpoint runs on a local
Ollama with no key and no hosted service: pull the checkpoint's Ollama tag, then point
`engine-b`'s `litellm_model` at `ollama/<tag>` and set its `api_key_env` to `null`. That is a
configuration change, not a code change, and the interface still offers exactly two Engines.
We did not time the local lane ourselves and claim no measurement for it: every Engine B figure
in this README comes from the hosted checkpoint.

### Re-running without fetching

A Run never fetches. Discovery is the only step that touches the internet, and the two are
separate commands and separate records, so a second pass over the same Corpus makes no request
at all. Its Documents fetched count is 0 by contract, and a test holds the line: a network guard
makes every outbound socket raise for the length of a Run, and the Run still produces Mappings.

A second pass also runs no OCR. The first Run stores the text it extracted under a key made of
four things: the Document bytes, the version of the extraction and OCR logic, the OCR languages
loaded, and the OCR policy in force. A later Run over the same Corpus rebuilds that key, finds
the stored stream and reads it instead of opening the Document again. Nothing is re-rasterised
and no OCR engine starts, which is why the second pass is the fast one on a scanned Corpus. If
any of the four parts moves, the key misses and the Document is read again rather than served
stale.

A reviewer sees it in two places. The Run Record's `extraction` map names each Document as
`extracted` or `reused`, beside the key that was looked up. The audit trail keeps its usual row
per stage per Document either way, and on a reused Document the method reads `reuse:<key>`
rather than an extractor name, on the M1 row and on the M2 row where OCR had fired the first
time.

In the interface: **Start a Run** → press **Start Run** again with a different Engine, Pillar or
Indicator. There is no offline toggle to find, because there is no fetching path to turn off:
the **Discover** button appears only when a Run was refused for an empty Corpus.

Where downloaded Documents are cached: inside the container `/data` (the named volume
`regcompass-data`, holding the working database, the Document bytes and the exports). On a host
install, the directory named by `REGCOMPASS_DATA`, default `data/`.

**Clearing it.** After a clear there is nothing left to reuse, so the next Run fetches and
extracts again from scratch. On the **Settings** screen, section **Clear downloads and cache**:
pick an Economy or **Everything**, press **Preview** to see the counts, then **Clear now** and
confirm. It removes the Corpus rows and their bytes under the data root, the stored text streams
and the Run Records with their Mappings and Review Decisions, for that scope only, and never
touches a path outside the data root. The same operation from the command line, for a runbook:

    regcompass clear --economy SG          # one Economy, with a typed confirmation
    regcompass clear --all --yes           # everything, no prompt

Do not run the command against a database a running server holds open: it cannot see whether a
Run or a Discovery is in flight. Use the **Settings** screen for a live server, which refuses
while a job is active.

---

## Crawling Politely

Built in and **on by default**. A ministry running this tool should not have to configure it to
avoid being blocked. The **Settings** screen shows these limits per Portal on the read-only card
**Polite crawling**, read from the same configuration the crawler runs on; nothing there can be
changed.

| Setting | Value | Where it is set |
| :---- | :---- | :---- |
| Max requests per second per host | 1 request per `min_interval_seconds`, the per-Portal minimum wait between two requests to the same host. The configured floors are 2 s (Thailand, Lao PDR, India, Viet Nam, Kazakhstan, Mongolia, Russian Federation), 3 s (China), 5 s (Malaysia, Indonesia), 6 s (Singapore) and 10 s (Australia); a Portal with no floor of its own waits the default 1.0 s. A Portal's published crawl-delay RAISES the floor and never lowers it. | default `src/regcompass/contracts.py:606`, per Portal `config/portals.yaml`, applied `src/regcompass/discovery.py:376` (the floor) and `src/regcompass/discovery.py:408` (raised by a published crawl-delay) |
| Parallel requests per host | 1 | `src/regcompass/crawl.py:586` (`httpx.Limits(max_connections=CONNECTIONS_PER_HOST, max_keepalive_connections=CONNECTIONS_PER_HOST)`, with `CONNECTIONS_PER_HOST = 1` at `src/regcompass/contracts.py:573`) |
| robots.txt respected | yes | `src/regcompass/crawl.py:401` (`read_robots_policy`, the one door every fetching lane uses), RFC 9309 longest-match rule at `src/regcompass/crawl.py:269` (`RobotsPolicy.allows`) |

Requests go out under an identified user agent naming the project and a contact URL. Documents
already in the Corpus are skipped without a request.

**When a Portal cannot show its own rules.** A `robots.txt` that answers 4xx means the Portal
published no rules, and Discovery proceeds at our own floor. A 5xx is a different answer: the
rules may exist and the Portal cannot show them, so Discovery refuses rather than take the
convenient reading. RFC 9309 section 2.3.1.4 lets a crawler treat a `robots.txt` that has been
failing for 30 days as publishing no rules, so each Portal carries the date a person checked it
by hand and found it broken (`robots_unreachable_since` in `config/portals.yaml`). Before
30 days the tool refuses and names the date the refusal lifts; after 30 days it fetches and
writes the reason onto the Discovery record, in plain words, so a reviewer can see the rules
were never read. Malaysia has been failing since 6 July 2026 and is past the 30 days. Adding a
Document by URL applies the same rule; uploading a file consults none of it, because an upload
makes no request.

**One Portal may be told to proceed instead.** A Portal can carry
`robots_unavailable_policy: proceed` in `config/portals.yaml`, an operator's recorded decision
that an unavailable `robots.txt` on THAT Portal is read as no rules published. It changes
nothing else: the Portal's spacing floor, the host whitelist, the identified user agent and any
rules the Portal can actually serve all apply as before, and nothing is silent, because the
Discovery record and the add-by-URL answer both carry the status `robots.txt` gave and the
policy that let the request out. India carries it, by a decision taken on 17 September 2026,
because its Portal answered HTTP 500 and then 502 on 16 September 2026 and the default refusal
would have stood until 16 October 2026, the day after the finale. Deleting that one line
returns India to the default refusal. Every other Portal is on the default.

Two Portals refuse our identified agent at the edge and are reached through a documented
escalation ladder, cheapest rung first, each rung smoke-tested rather than assumed: ask as
ourselves, and only after a refusal climb to a browser-impersonating rung, recording on the
Discovery record that we did. Four further Portals were left as manual on purpose, because the
only remaining route would have routed around rules those Portals publish. Those decisions are
in the Notes column of **Supported Economies and Portals**.

---

## Architecture Overview

The boundary that matters is between fetching and reading. Discovery writes the Corpus and is
the only step that touches the internet. A Run reads the Corpus and fetches nothing.

```
Economy                                    Economy + Pillar (+ Indicators) + Engine
   |                                                     |
   v                                                     v
DISCOVERY (the only step on the network)         RUN (never touches the network)
  discovery.py  plans and records                  extract.py  canonical text stream per
  crawl.py      identified agent, robots.txt,                  Document (the byte authority)
                one connection, spacing floor,     ocr.py      auto-fires on low-yield pages:
                sha256 manifest                                tesseract -> RapidOCR ladder
       |                                           chunk.py    section-aware chunking
       v                                           gate.py     bge-m3 meaning tier + BM25
+--------------------+                                         keyword tier
|      CORPUS        |  ---------------------->    map.py      the Engine SELECTS a quote
|  Document bytes,   |     read, never fetched     verify.py   byte-for-byte substring check
|  Source URL,       |                                         (the mechanical guarantee)
|  Language          |                             reconcile.py controlling Mapping per cell
+--------------------+                             classify.py  rubric-derived scores
       ^                                                 |
       |  upload or Add by URL                           v
  a reviewer                                    REVIEW and EXPORT
                                                  server.py + ui/  accept, reject, flag
                                                  export.py        the organizer's workbook
                                                        |
                                                        v
                                    submission.xlsx (the organizer's own workbook, filled)
                                    + submission.csv + submission.json + supplementary.json
                                    + a SQLite audit trail of every decision
```

The mechanical guarantee, in five steps. (1) One canonical text stream per Document; chunks are
SLICED from it by character offsets, never re-emitted by a model. (2) The Engine may only return
a quote and an Indicator under a strict schema at temperature 0, with transport retries pinned
to 0. (3) The verifier byte-compares the quote against the canonical stream; a failure gets a
stricter retry (3 attempts total), then a logged drop. (4) Reconciliation picks one controlling
provision per (Economy, Indicator), and a pointer gate asserts its Article / Section names the
Document's own heading at the quote's exact position. (5) Every stage logs to `audit_log`.

### Key modules

| Module | File | Description |
| :---- | :---- | :---- |
| Portal Crawler | `src/regcompass/discovery.py`, `src/regcompass/crawl.py` | Reads Portals, records what it fetched and what it refused. The only network door. |
| Document Processor | `src/regcompass/extract.py`, `src/regcompass/ocr.py`, `src/regcompass/chunk.py` | Download, OCR, structural parsing, section-aware chunking |
| Retrieval | `src/regcompass/gate.py`, `src/regcompass/shortlist.py` | Chunking-aware shortlist, embedding, keyword search, ranking |
| Mapper | `src/regcompass/map.py`, `src/regcompass/verify.py`, `src/regcompass/reconcile.py` | Maps a provision to an RDTII Indicator, then proves and reconciles it |
| Interface | `src/regcompass/server.py`, `ui/src/` | Run control, audit view, review, comparison, export |
| Output Writer | `src/regcompass/export.py`, `src/regcompass/workbook.py` | Writes the RDTII schema into the organizer's own workbook |

The pipeline wiring lives in `src/regcompass/pipeline.py` and `src/regcompass/cli.py`; Engine
resolution in `src/regcompass/engines.py`; state in `src/regcompass/storage.py` (SQLite; a
PostgreSQL and pgvector swap ships in `pg.py`, see `docs/POSTGRES.md`).

---

## Swapping the OCR Engine

| Engine | Config value | Notes |
| :---- | :---- | :---- |
| Tesseract (first rung, the default) | vendored language data in `vendor/tessdata/`, chosen per Document by its Language in `src/regcompass/languages.py` | Open source, no key, no network. Apache-2.0. |
| RapidOCR (escalation rung) | automatic on pages below the confidence or dictionary-hit thresholds | Open source, no key, no network. Runs for scripts it has a model for (Latin, Chinese) and is off for the rest. |
| Manual review (final rung) | automatic | A page neither engine could read is flagged for a person with the reason attached, never silently passed as text. |

**None of these is a proprietary service.** The whole OCR path runs offline, which is what makes
the Section 3 declaration hold for OCR as well as for the language model. The same is true of
the Gate embedder (`bge-m3` on a local Ollama) and of translation.

Every OCR-processed page saves an (input PNG, extracted text) evidence pair, so a reviewer can
compute error rates independently.

The verdict travels with the Document rather than with the run that produced it. Whichever lane
wrote the Corpus row, the upload and the Run alike, it records the mean word confidence, the
dictionary hit rate where the script allows one, whether the RapidOCR rung was reached, and
whether a person was asked to look. The export reads those columns for the submission's
`ocr_quality` block, so an escalation is a recorded fact and not something to be inferred from
the extractor's name.

### Vendored language data (artifact table)

Redistributed unmodified from `tessdata_best`
(https://github.com/tesseract-ocr/tessdata_best), Apache-2.0. Every file is pinned here and
`tests/test_vendored_assets.py` hashes the checked-in bytes against this table. `eng`, `msa` and
`lao` were vendored earlier and their provenance is the digest below; `ind`, `tha` and `rus`
were taken from tag `4.1.0` and each was verified against that tag's GitHub blob SHA on
download.

| File | Language | Bytes | SHA-256 | Source |
|---|---|---|---|---|
| `vendor/tessdata/eng.traineddata` | English | 15,400,601 | `8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba` | vendored earlier |
| `vendor/tessdata/msa.traineddata` | Malay ("Other", Malaysia) | 8,230,552 | `2fa693701d2fb3685cf3e0a64bc1bfb97d66a50f93c07b2ebc0ff360ea62621f` | vendored earlier |
| `vendor/tessdata/lao.traineddata` | Lao | 13,532,551 | `12680f24af571909d69f0dd0151ecdf657a98f000c0ca011c11f8f9dc2bf69b7` | vendored earlier |
| `vendor/tessdata/ind.traineddata` | Bahasa Indonesia | 8,253,606 | `1f6596041ffb4cd5094e5f98764db43cfde04edb8f02b988f90ebc1353ac73b8` | tag 4.1.0, blob SHA verified |
| `vendor/tessdata/tha.traineddata` | Thai | 7,614,571 | `ee8adab6dc69eb8df3d3c8307ae8295471b7bd7d86a06d9267aa8f479b064eac` | tag 4.1.0, blob SHA verified |
| `vendor/tessdata/rus.traineddata` | Russian | 15,301,764 | `b617eb6830ffabaaa795dd87ea7fd251adfe9cf0efe05eb9a2e8128b7728d6b6` | tag 4.1.0, blob SHA verified |

All six files are inside the Docker image and are tested in the container.

**Languages with no vendored data: Chinese, Vietnamese, Hindi, Kazakh and Mongolian.** A scanned
page in one of those is read as English by the first rung, so rather than present the result
confidently the page is forced to manual review with that reason attached, whatever confidence
tesseract reports over the misread glyphs. A PDF with a real text layer works normally in all
five, because no OCR is needed. Adding one of them is a file, a row in the table above, and a
row in `languages.TESSERACT_BY_LANGUAGE`.

**Chinese is the one of the five that the second rung rescues.** RapidOCR has a Chinese model,
so a Chinese scan escalates and comes back readable: a 7-page scan of the Personal Information
Protection Law, read on 22 September 2026, gave 7,942 clean characters. The manual-review flag
still stands, because no Chinese data is vendored for the first rung and the flag is about what
this install can vouch for, not about what the text turned out to be. Everything downstream then
works on that text: the chunker's `article_zh` profile reads `第N条` article headings and `第N章`
chapters, the Gate takes its meaning-only lane, and every row carries a labelled English
rendering of its Chinese quote.

---

## Supported Economies and Portals

| Economy | Official portal | Language | Run end to end? | Notes |
| :---- | :---- | :---- | :---- | :---- |
| Australia | `www.legislation.gov.au` | English | **Yes.** Corpus fetched and checked; Runs and Exports green. | Genuine JavaScript application, so Discovery needs the headless browser. The image carries it, so this Portal runs in the container as well as on the host. |
| Malaysia | `lom.agc.gov.my` | English (Malay editions) | **Yes** from the held Corpus; Discovery is broken upstream. | Since 16 Sep 2026 the Portal's search answers with an encrypted payload instead of a result set, so Discovery finds nothing and says so. The printed host `lor.agc.gov.my` is dead; `lom.agc.gov.my` is live. Malay is not one of the organizer's eleven values, so a Malay Document exports as "Other". |
| Singapore | `sso.agc.gov.sg` | English | **Yes.** Corpus fetched and checked; Runs and Exports green. | Escalation ladder after the WAF refuses the identified agent. Document is the whole-act consolidation PDF. |
| Indonesia | `peraturan.bpk.go.id` | Bahasa Indonesia, English | **Yes.** Live Discovery on 23 Sep 2026 fetched 7 Documents; pre-run on both Engines, Pillars 6 and 7. | The Audit Board's national regulation database. Its rules permit every route we take and publish no crawl-delay, while its edge answers our identified agent with 403, so it takes the same escalation ladder Singapore takes. `peraturan.go.id` is whitelisted but unreachable from our network. |
| Thailand | `searchlaw.ocs.go.th` | Thai, English | **No Discovery.** Add by URL or upload. | The host printed in our own plan, `www.krisdika.go.th`, is a dead placeholder; the live Council of State law library is `searchlaw.ocs.go.th`, and its certificate verifies cleanly, so the planned certificate pinning was not needed and was not added. Its browse lane answers us and names the statutes we want, but all three of its search services fail with the same error and both of its law-text services reject the identifier the browse lane gives, so there is no path from a seed to a Thai statute today. Verbatim answers in `tests/fixtures/portals/th/`. Thai OCR data is vendored, so an uploaded Thai scan reads normally. |
| Lao PDR | `laoofficialgazette.gov.la` | Lao, English | **Discovery yes, Runs not yet.** Live Discovery on 23 Sep 2026 fetched 37 Documents; not pre-run. | Server-rendered gazette. `/robots.txt` answers 200 with the homepage (a soft 404), so no rules are published and our own 2 s floor stands. The Lao PDF is the Document; an English rendering beside it is recorded in Notes and is never the quote source. |
| Viet Nam | `vbpl.vn` | Vietnamese, English | **No Discovery.** Add by URL or upload. | The Portal publishes real rules and a sitemap, but the sitemap is four navigation pages and the document list exists only behind `/api/`, which those rules forbid. A headless browser would make the forbidden calls itself, so we declined it. No Vietnamese OCR data: text-layer PDFs only. |
| China | `www.cac.gov.cn` | Chinese | **Yes**, from three statutes added by Source URL; no Discovery. Pre-run on both Engines, Pillars 6 and 7. | The Cyberspace Administration of China, the regulator that enforces the Personal Information Protection Law, the Data Security Law and the Cybersecurity Law, republishes the National People's Congress text in full and its rules permit the law pages, so each statute is added by its Source URL on that host. The national law database `flk.npc.gov.cn` forbids any automated collection in its `robots.txt`, so it is never whitelisted or requested; `www.npc.gov.cn` refused HTTPS and stalled over HTTP. A scan uploaded by hand also works: RapidOCR reads Chinese, the chunker reads `第N条` articles, and the rows ship with Language of Source `Chinese` and a labelled English rendering; a scanned page still carries the manual-review flag, because no Chinese data is vendored for the first OCR rung. |
| India | `indiacode.gov.in` | English, Hindi | **Yes.** Live Discovery on 23 Sep 2026 fetched 4 Documents; the Information Technology Act, 2000 was uploaded with its official India Code Source URL, because Discovery's title search does not reach it; pre-run on 5 Documents, both Engines, Pillars 6 and 7. | The Portal moved from the printed host and now exposes a read-only DSpace REST interface, which the adapter reads. Its `robots.txt` answered HTTP 500 then 502 on 16 Sep 2026, so it can show no rules; India carries `robots_unavailable_policy: proceed` in `config/portals.yaml` (operator decision, 17 Sep 2026), so Discovery and the add-by-URL lane go ahead at the 2 s floor and every record says the status and the policy. Delete that line to return to the default refusal. No Hindi OCR data: text-layer PDFs only. |
| Kazakhstan | `adilet.zan.kz` | Russian, Kazakh | **No Discovery.** Add by URL or upload. | The act route answers a small shell whose own comment says the full text is withheld from non-browser clients on purpose, so that the system is cited rather than drained. A headless browser would render around that ask, so we do not. Russian OCR data is vendored; no Kazakh data. |
| Mongolia | `legalinfo.mn` | Mongolian, English | **No Discovery.** Add by URL or upload. | Every act list, search included, arrives through an undocumented POST endpoint returning pre-rendered markup; the page advertised as an API reference is an article, not an API. No Mongolian OCR data: prefer a text-layer PDF or the English tree. |
| Russian Federation | none verified | Russian | **No Discovery.** Add by URL or upload. | `publication.pravo.gov.ru` answers only over port 80 from our network (port 443 is blocked on the path). Its acts are image-only scanned PDFs and it publishes amendments, not consolidated law, so no host is whitelisted and the former Thailand fallback was not taken. Russian OCR data is vendored. |

The Portal list is a whitelist enforced at export: a row whose Source URL host is not one of
these fails the export battery unless the reviewer explicitly marked the Document as an official
source outside the configured Portal, which stamps a disclosure into its Notes. Per-Portal
endpoints, URL shapes and verified quirks: `docs/PORTALS.md`.

---

## Pre-run Coverage

Before submission we ran the six Economies the team chose, Pillars 6 and 7, on both declared
Engines: 24 Runs, one per Economy, Pillar and Engine, driven by `scripts/pre_run.py`. Every
figure below comes from the Run Records those Runs wrote (the job copies each finished Run into
its ledger, and `uv run python scripts/pre_run.py --report` prints this table from it). Nothing
here is an estimate. Engine A is `openai/gpt-5.6-luna`, Engine B is
`qwen/qwen3-30b-a3b-instruct-2507`, both through OpenRouter.

| Economy | Pillar | Documents | Engine A Mappings | Engine A USD | Engine A minutes | Engine B Mappings | Engine B USD | Engine B minutes |
| :---- | :---- | ----: | ----: | ----: | ----: | ----: | ----: | ----: |
| Australia | 6 | 18 | 43 | 0.6005 | 20.2 | 399 | 0.1729 | 47.8 |
| Australia | 7 | 18 | 317 | 1.2622 | 34.5 | 717 | 0.3538 | 74.7 |
| Malaysia | 6 | 21 | 14 | 0.1334 | 4.7 | 76 | 0.0310 | 37.2 |
| Malaysia | 7 | 21 | 116 | 0.3866 | 12.5 | 285 | 0.0861 | 23.1 |
| Singapore | 6 | 10 | 12 | 0.2514 | 10.2 | 156 | 0.0681 | 18.6 |
| Singapore | 7 | 10 | 148 | 0.5822 | 18.2 | 390 | 0.1341 | 34.5 |
| China | 6 | 3 | 35 | 0.1254 | 5.9 | 55 | 0.0298 | 8.8 |
| China | 7 | 3 | 75 | 0.1878 | 9.0 | 72 | 0.0365 | 13.7 |
| Indonesia | 6 | 7 | 18 | 0.2036 | 6.6 | 66 | 0.0499 | 15.8 |
| Indonesia | 7 | 7 | 107 | 0.3835 | 13.1 | 158 | 0.0815 | 25.9 |
| India | 6 | 5 | 7 | 0.0997 | 3.3 | 53 | 0.0264 | 4.8 |
| India | 7 | 5 | 70 | 0.2395 | 9.0 | 128 | 0.0522 | 9.2 |
| **Total, 12 Runs per Engine** | | **64** (each read once per Pillar) | **962** | **4.4558** | **147.2** | **2,555** | **1.1224** | **314.3** |

How to read it.

- **Documents** is the Corpus the Run read: Australia 18, Malaysia 21, Singapore 10, China 3,
  Indonesia 7, India 5, so 64 Documents, each read once per Pillar. Australia, Malaysia and
  Singapore are the Round 1 Corpus fetched from their Portals; Indonesia was fetched by live
  Discovery on 23 September 2026.
- **India's five Documents** are 4 fetched by live Discovery on 23 September 2026 and the
  principal Information Technology Act, 2000 (Act No. 21 of 2000, India Code's consolidated
  text "[As on the 27th June, 2025]", 44 pages, English), uploaded with its official India Code
  Source URL. Discovery misses that Act: India Code's title search returns only state copies without a
  PDF among its top results, and Discovery reports the family as carrying no PDF in its ORIGINAL
  bundle rather than guessing. Its rows export like any other; the Act alone yields 29 verified
  Mappings on Engine A and 53 on Engine B.
- **China's three statutes** (the Personal Information Protection Law, the Data Security Law and
  the Cybersecurity Law, the last as amended on 28 October 2025) come from the regulator that
  enforces them, the Cyberspace Administration of China at `www.cac.gov.cn`, which republishes
  the National People's Congress text in full. The national law database `flk.npc.gov.cn`
  forbids automated collection in its `robots.txt`, so we never request it, and `www.npc.gov.cn`
  refused HTTPS and stalled over HTTP. Every China row therefore carries an official Source URL
  and exports like any other.
- **Mappings** are the verified records each Run kept. Engine A keeps far fewer than Engine B
  (962 against 2,555): on most candidate passages it answers, well formed and on the first
  attempt, that the passage is not evidence for the Indicator. That is a difference between the
  Engines, not a failure, and the **Comparison** screen puts the two side by side per Indicator.
- **USD** is our own meter: the tokens each model response reports, priced at the per-million
  prices declared in `config/models.yaml`. The provider bills slightly above our meter; its own
  figure is stored beside ours on every Run Record, so the two can be compared.
- **Minutes** is each Run's own wall time. The Engine B Runs ran one after another; the Engine A
  Runs, and the China and India re-runs on both Engines, ran several at once, so their minutes overlap and
  do not add up to elapsed time (the first Engine A pass over all twelve combinations took about
  39 minutes from start to finish).
- **Total spend:** USD 5.5782 metered for the 24 Runs above. The ledger also holds eight
  earlier China Runs (USD 0.4907), from before China's Corpus moved to the regulator's site and
  before its articles were split correctly, and four earlier India Runs (USD 0.3032), from
  before the Information Technology Act, 2000 joined its Corpus, for USD 6.3720 across 36 Runs
  in the ledger. Three attempts that we stopped part way (two hung provider requests
  and one Corpus path failure, about USD 0.30) are outside the ledger; their Run Records read
  **interrupted**.
- **Lao PDR** has its Corpus (37 Documents, live Discovery on 23 September 2026) but was not
  pre-run, so it has no Runs here.
- **India's Hindi rendering of the Telecommunications Act 2023** is in its Corpus but its text
  layer extracts as unreadable characters and too few pages fall below the OCR threshold to
  trigger OCR, so it yields no Mappings. The Consumer Protection Act 2019, which we looked for,
  carries no PDF on India Code, and Discovery reports it as not found rather than guessing.

---

## Output Format

Columns are in this exact order. The workbook written is the organizer's own final-round
template, vendored byte for byte at `config/organizer/` with its SHA-256 pinned by a test, and
filled in place so its Pillar formulas, validations, autofilter and Coverage Matrix keep
working.

| # | Column | Required | Description |
| :---- | :---- | :---- | :---- |
| 1 | economy | Required | Official UN country name, in the Coverage Matrix spelling. Written as `Economy`. |
| 2 | law_name | Required | Full official statute name and year. Written as `Law Name`. |
| 3 | law_number_ref | Optional | Official act or law number (e.g. Act 709, B.E. 2562). Written as `Law Number / Ref`. |
| 4 | last_amended | Optional | Year of most recent amendment; blank if the Document declares none. Written as `Last Amended`. |
| 5 | indicator_id | Required | **RDTII 2.1 code as text: `6.1`, `7.3`, `12.9`. Not "P6-I1".** Written as `Indicator ID`. |
| 6 | article | Required | Exact article and paragraph at the quote's position. Written as `Article / Section`. |
| 7 | discovery_tag | Required | NEW = independent find; KNOWN = in the baseline we hold. Written as `Discovery Tag`. |
| 8 | location_reference | Optional | PDF page number, or HTML anchor / section path. Written as `Location Reference`. |
| 9 | verbatim_snippet | Required | Exact quoted text, byte-verified, no paraphrasing. Written as `Verbatim Snippet`. |
| 10 | mapping_rationale | Optional | Max 300 characters: why this provision maps to this Indicator. Written as `Mapping Rationale`. |
| 11 | source_url | Required | Direct URL on the official government Portal. Written as `Source URL`. |
| 12 | confidence | Optional | Mechanical composite (0.00 to 1.00), never model self-reported. Written as `Confidence`. |
| 13 | notes | Optional | OCR issues, bilingual sources, cross-references, disclosures. Written as `Notes`. |
| 14 | language_of_source | Required | Original language of the Document, from the organizer's eleven values. Written as `Language of Source`, the organizer's own spelling. |

> **Indicator IDs are written as text cells**, so `12.10` stays `12.10` and `4.01` stays `4.01`.
> Entered as numbers they would collapse to `12.1` and `4.1`, which are different Indicators.

Each Export writes the organizer's workbook (`submission.xlsx`) and a CSV (`submission.csv`)
carrying the same rows, plus `submission.json` (the rows with pinned model identities,
per-Document OCR quality and context windows around each quote) and `supplementary.json` (the
derived scores, the battery result and every disclosure). **All four are download links in the
interface** the moment the Export finishes, workbook first: press **Export workbook** on the
**Evidence** screen and the footer offers them. On the supported Docker path the output
directory lives inside a named volume, so the link is how a reviewer gets the file, and
`docker cp` is never needed. After column 14 the CSV appends four
enrichment columns, never inserted between the contracted ones: **Novelty Scope**, **Controlling
Evidence**, **Relationship To Group** and **Verbatim English**. Two Exports of the same database
are byte-identical.

Rows land in Output Data from row 9 and never past row 109, at most 101 provision rows; over the
cap the Economies take turns by Confidence and the Export says how many rows were left out.
"No provision found" absence rows are CSV-only, because each workbook row counts as a provision
in the organizer's Coverage Matrix.

**One row per provision.** A Run can produce two accepted Mappings describing the same provision
under the same Indicator, because the Gate shortlists two neighbouring chunks of one Document
and the quote-anchored label repair re-derives both labels to the same nearest heading. The
Export collapses such rows on (Economy, Indicator ID, Law Name, Article / Section), keeping
Controlling Evidence first, then the higher Confidence, then the earlier Mapping id. Nothing is
deleted: the Review Decisions stay in the database and the disclosure is reported in the Export
summary and in `supplementary.json`.

**Corrected rows.** A Mapping a reviewer corrected ships under the corrected Indicator and its
Pillar, with the reviewer's reason as its Mapping Rationale, the pipeline's Confidence, and Notes
reading *Reviewer override: Engine proposed X; corrected to Y by &lt;reviewer&gt;*. The Indicator the
Engine proposed gets no evidence from it: there it counts as a verified Mapping found but not
accepted, which that Indicator's "No provision found" row says when nothing else was accepted
for it. `supplementary.json` lists `n_corrected` and every override
under `review_gate`.

**The battery that must be green before anything ships.** Every assembled row is checked for the
13-column contract by name and order (the contract counts 13 because the Round 1 columns are
positionally frozen in `src/regcompass/export.py:66` (`COLUMNS`), and `Language of Source`, the final round's
one new organizer column, is appended after them at position 14 rather than inserted among them);
Discovery Tag exactly NEW or KNOWN, case-sensitive; no
empty required column; a re-check that the Verbatim Quote is still a substring of its source
chunk; the Source URL host on the Portal whitelist, or exempted with its Notes disclosure
present; the Source URL live; a repealed instrument flagged in Notes; no leftover template
example content; the Mapping Rationale within 300 characters; Confidence inside 0.00 to 1.00; a
non-English Verbatim Quote carrying a non-empty English rendering, labelled non-authoritative
unless a named reviewer approved it; the section label matching the Document's own heading on
controlling rows; and no duplicate provision-Indicator row. A red battery ships nothing.

---

## Measured Cost

**Measured costs from real runs, not estimates.**

| Component | Engine used | Measured cost |
| :---- | :---- | :---- |
| OCR | local tesseract, then RapidOCR | $0.00 |
| Embedding | local `ollama/bge-m3` | $0.00 |
| Mapping - Engine A | `openai/gpt-5.6-luna` | $0.0348 per document per Pillar |
| Mapping - Engine B | `qwen/qwen3-30b-a3b-instruct-2507` | $0.0088 per document per Pillar |
| Crawling | plain HTTP from this machine | $0.00 |
| **Total, Engine A** | | **$0.0348 per document** (per Pillar) |
| **Total, Engine B** | | **$0.0088 per document** (per Pillar) |

**Measured on:** 22 and 23 September 2026, a macOS laptop (Apple silicon), the pre-run of
**Pre-run Coverage**. **Benchmark document:** the whole pre-run Corpus rather than one law: 64
Documents from six Economies, each read once for Pillar 6 and once for Pillar 7 (128
Document-Pillar reads per Engine), from a 3-document China set to Malaysia's 21.
**Wall-clock:** 69 seconds per document per Pillar on Engine A, 147 on Engine B.

The working: Engine A's 24-Run meter reads USD 4.4558 over 128 Document-Pillar reads, so
USD 0.0348; Engine B's reads USD 1.1224 over the same 128, so USD 0.0088. Wall-clock is the
Runs' summed wall time over the same 128: 147.2 minutes on Engine A and 314.3 on Engine B. The
first Engine B pass ran one Run at a time; the Engine A Runs, and the China and India re-runs on
both Engines, ran several at once, sharing the machine and the provider, so these figures are per
Run and not strictly sequential ones. The cost per Document
varies with its length and with how many passages the Gate passes to the Engine: on Engine A it
ranged from USD 0.0124 (Malaysia) to USD 0.0522 (China), on Engine B from USD 0.0028
(Malaysia) to USD 0.0146 (Australia). These are our meter's figures; the provider bills slightly
above them (see **Pre-run Coverage**).

Three rows of that table are already zero by construction, and the code shows why. **OCR,
embedding and crawling cost $0.00**: tesseract and RapidOCR run locally, the Gate embedder is
`ollama/bge-m3` with `api_key_env: null`, and Discovery makes plain HTTP requests. Only the
Mapping rows can be non-zero.

**Where a reviewer reads the cost, per Run and per Engine, without doing arithmetic.** Every Run
writes a Run Record holding its Engine, its start and end time, the prompt and completion tokens
summed from each model response's own usage figures, and the dollar cost those tokens come to at
the Engine's declared per-million prices in `config/models.yaml`. OpenRouter's own reported cost
is stored beside ours, so the two can be compared. Read it in the interface on the **Run
history** screen, column **Cost USD**, beside column **Tokens** (read in and written out) and a
per-Run **Record** download link, or in the header of the Run view; at the command line with `regcompass runs` and `regcompass runs show <run_id>`; over the
API at `/api/runs` and `/api/runs/{id}`. The metering sits at the one place every model call
passes through, and a test recomputes it independently.

Declared prices, per million tokens (`config/models.yaml`): Engine A $0.20 input and $1.20
output; Engine B $0.05 input and $0.19 output.

The per-document figures above are divided out of the Run Records, not estimated from prices
and token counts; the per-Economy ones are in **Pre-run Coverage**.

---

## Known Limitations

- **Scanned documents in a Language with no vendored OCR data.** Chinese, Vietnamese, Hindi,
  Kazakh and Mongolian have no traineddata here. A scanned page in one of them is flagged for
  manual review rather than read as English, so four of those five Economies need text-layer
  PDFs. Chinese is the exception: RapidOCR reads it on the escalation rung, so a Chinese scan
  runs end to end and keeps its flag. All five are in the live-test pool.
- **OCR fragility on scans generally.** The OCR ladder saves an evidence pair per page and flags
  what it could not read, but a badly scanned page produces a page that is flagged, not a page
  that is mapped. A measured character error rate is reported as null rather than guessed,
  because it needs a hand-checked reference.
- **Portals that cannot be crawled.** Six of the twelve Economies have no Discovery today, for
  reasons recorded per Portal: three fixed statutes on a regulator's host, while the national law
  database forbids automated collection (China), rules that forbid the only route to
  the document list (Viet Nam, Kazakhstan), no published route at all (Mongolia), a network path
  that blocks HTTPS and a Portal that serves only scanned amendments (Russian Federation), and a Portal whose search and law-text services both fail
  for its own application's requests (Thailand). India's Portal cannot serve its own `robots.txt`, and it discovers under the
  configured policy that says what that silence means for it, disclosed on every record.
  Malaysia's Portal search broke upstream on 16 September 2026. In every one of these cases a
  reviewer can still upload the Document or add it by URL, and everything after that is
  unchanged.
- **Duplicate provision rows are collapsed.** The quote-anchored section-label repair re-derives
  a record's label from the nearest heading at the quote's position. Where a chunk carries text
  above its own section heading, which is what the chunk opening a new Part looks like, a quote
  from those lines is no longer relabelled to the last section of the previous Part: the chunk's
  own label stands and the export gate records the row for eyeballing. A chunk cut in the middle
  of a section is unaffected, because the heading above it is genuinely its own. Measured on the
  offline Engine at Pillar 7 this removed every duplicate the fixture Economies produced
  (Australia 0 collapses, Malaysia 3 to 0, Singapore 4 to 0, Lao PDR 0). The collapse stays as
  the final guard, because a long section split across two chunks and quoted in both still yields
  two rows on one provision; it keeps the workbook at one row per provision, and the audit view
  shows each quote at its exact position in the source PDF.
- **Delegated legislation.** Primary statutes are mapped; cross-references into subordinate
  regulations are not followed automatically.
- **Non-English evidence is quoted in its own language.** The Verbatim Quote always stays in the
  source language. The English rendering beside it is machine-drafted and ships under a
  non-authoritative label unless a named person has approved it; that rule is mechanical, not a
  convention.
- **Derived scores are an approximation.** The 14-column table is the deliverable and has no
  score column. The derived scores in `supplementary.json` are a documented
  presence-plus-classification approximation, and the judged `regcompass export` command emits
  the earlier presence-only version of them; the reproduction script emits the rubric version.
- **Confidence calibration.** Confidence is **relative, not a calibrated probability**. It is a
  mechanical composite of four model-independent anchoring signals with documented weights
  (`src/regcompass/export.py:403`, `confidence_score`): Gate cosine similarity 0.45, quote length 0.25 saturating at
  240 characters, a multi-Indicator specificity penalty 0.15, and a retry penalty 0.15. It never
  contains a model's own self-reported certainty; the schema has no field for one. Read it as a
  review queue order, not as a probability that the row is right. On the Evidence screen,
  **How this is scored** beside the dots shows each signal's value, weight and contribution. **Our rule of thumb, not a
  calibrated threshold: check every row below 0.60 by hand.** The Evidence screen's **Review
  queue** view is that rule made operable: it lists the Run's records lowest first and counts how
  many sit below the threshold, so those rows are the ones a reviewer meets first. The number
  lives in one place in the code (`REVIEW_CONFIDENCE_THRESHOLD` in `src/regcompass/export.py`) and
  a test holds it and this paragraph together. A row falls that low only when its
  Gate similarity sits near the bottom of the calibrated 0.35 to 0.75 cosine band, or when
  several of the four signals went badly at once. Retries cap the score arithmetically whatever
  else it scores: at most 0.94 after one stricter retry and 0.90 after two.

---

## Running the Test Suite

    uv sync --extra live
    uv run pytest

**2,324 tests**, offline and keyless. Tests that would spend money skip unless
`REGCOMPASS_PAID=1` is set alongside a key, so a plain run never bills an account. Live Portal
tests skip unless `REGCOMPASS_LIVE=1` is set, so a plain run never touches a government server.
Gate tests probe for a reachable Ollama and skip when there is none. Docker tests that need a
real image build skip unless `REGCOMPASS_DOCKER_BUILD=1` is set.

Two checks drive a real browser over the built interface and live beside the suite rather than
inside it, because they need Playwright and its Chromium:

    uv run python scripts/u0_e2e.py           # the review flow, the Export downloads, the screens
    uv run python scripts/pdf_render_check.py # a scanned law really renders in the audit view

Both are offline and keyless, and both drive the committed bundle, so run `npm run build` in
`ui/` first if the React sources have moved ahead of it.

| Test file | What it tests |
| :---- | :---- |
| `tests/test_add_document.py` | A reviewer adds a Document by upload or by Source URL |
| `tests/test_audit.py` | The audit view: bundle loading, quote location, highlight rectangles, the accepted-only export gate |
| `tests/test_chunk.py` | Deterministic section splitting across four drafting styles, and the coverage partition invariant |
| `tests/test_classify.py` | The closed-menu classification schema and every branch of the scoring rubric |
| `tests/test_clear.py` | Clearing the downloads and every cache: the scope, the data-root fence, the endpoint, the command line, and a second pass before and after |
| `tests/test_cli_discover.py` | The two command-line lanes: `discover` fills a Corpus, `run` reads it |
| `tests/test_cli_seed.py` | `regcompass seed`, the offline step in front of the keyless demo |
| `tests/test_compare.py` | The Comparison: two Runs on one Economy and Pillar, side by side per Indicator |
| `tests/test_config.py` | Every committed config file loads through its contract model |
| `tests/test_contracts.py` | Seam models round-trip unchanged, and invariants fail at construction time |
| `tests/test_corpus_run.py` | A Run reads the Corpus and never fetches, proved with a network guard |
| `tests/test_crawl.py` | Manifest resume, SHA-256 dedupe, failed URLs recorded not retried blindly |
| `tests/test_discovery.py` | Discovery against recorded Portal answers, including "no request was made" |
| `tests/test_docker.py` | The image, the compose file and the health check a judge's one command depends on |
| `tests/test_e2e.py` | The end-to-end lane, the single-PDF lane and the off-corpus export fallback |
| `tests/test_engines.py` | The Engine registry: one named Engine drives every model-calling stage |
| `tests/test_evidence_goldens.py` | Every shipped evidence artifact is pinned by exact bytes |
| `tests/test_export.py` | The column contract, the gate battery and the goldens |
| `tests/test_export_duplicate_rows.py` | A real Run always exports, because duplicate provision rows collapse first |
| `tests/test_export_fixture_notes.py` | A seeded Corpus discloses itself in the Export |
| `tests/test_extract.py` | Page and word slice invariants, determinism, coverage against an independent baseline |
| `tests/test_extraction_reuse.py` | A Run reads a Document's text once and reuses it: the extraction key, the cache hit and miss, the Run Record's reused or extracted map |
| `tests/test_gate.py` | The two-tier Gate over real chunks, with recall ground truth from the ESCAP database |
| `tests/test_glosses.py` | English renderings drafted by the selected Engine, labelled and counted |
| `tests/test_gt_matrix_artifact.py` | The frozen 27-cell ground-truth comparison covers every cell and justifies every mismatch |
| `tests/test_gt_scores.py` | The published agreement number is the one the code computes |
| `tests/test_known_matrix_extract.py` | Section-reference parsing behind the NEW and KNOWN tags |
| `tests/test_languages.py` | Which OCR data a Language asks for, and which Gate tier can read it |
| `tests/test_lao_portal.py` | Lao PDR's gazette, replayed from recorded bytes |
| `tests/test_live_test_robustness.py` | The first upload on a fresh install, a Document with no structure, a Run left behind by a dead process, and an OCR escalation on the row |
| `tests/test_map.py` | The Engine selects a quote per (chunk, Indicator) pair, and the code anchors or rejects it |
| `tests/test_non_english_lane.py` | OCR in the Document's Language and the meaning-only Gate, end to end |
| `tests/test_ocr.py` | Both scanned fixtures with quality proxies, the escalation ladder and the manual-review flag |
| `tests/test_paid_optin.py` | Money never leaks into a bare test run |
| `tests/test_parallel_mapping.py` | Mapping calls with bounded concurrency: record order equals pair order, byte-identical export, rate-limit backoff, a hard failure keeps the other records |
| `tests/test_pg.py` | PostgreSQL and pgvector parity against the shipped seams (skips without a DSN) |
| `tests/test_pipeline.py` | The judged path: `regcompass run` and `regcompass export` wiring, offline |
| `tests/test_portal_id.py` | Indonesia's Portal, Discovery and the escalation that makes it possible |
| `tests/test_pre_run.py` | The pre-run job: resumable skip of completed Runs, the spend ceiling, the per-Run stop, the ledger and the coverage report |
| `tests/test_reconcile.py` | The legal-hierarchy ladder enforced in code, not by the model |
| `tests/test_registry.py` | Economies and Indicators are data: any Economy, any Pillar, no code change |
| `tests/test_repro_checkpoints.py` | Resume safety for the reproduction script's checkpoints |
| `tests/test_reviews.py` | Review Decisions survive a refresh, a restart and a migration |
| `tests/test_run_records.py` | Run Records: tokens and cost proved against a temporary registry |
| `tests/test_run_scoped_mappings.py` | A Run owns its Mappings, and a Run can be narrowed to Indicators |
| `tests/test_section_label_repair.py` | Quote-anchored section-label repair and the headings the chunker misses |
| `tests/test_seed.py` | A Corpus a judge can Run against with no network |
| `tests/test_server.py` | The one server: Start a Run API, Run history, Settings key, audit view, export |
| `tests/test_shortlist.py` | The whole-document shortlist ranker, with recall and precision measured |
| `tests/test_source_links.py` | Following a Mapping row to its official source: the page fragment, the local-copy fallback, the fields the interface reads |
| `tests/test_storage.py` | Schema application, audit logging and the embedding round-trip |
| `tests/test_translate.py` | The translation lane, its default-off flag and its degenerate-output guard |
| `tests/test_upload_collisions.py` | Several Documents may share one Source URL, and a Run killed the moment it starts still leaves a record |
| `tests/test_vendored_assets.py` | The vendored OCR language data is exactly what this README says it is |
| `tests/test_verify.py` | The byte-for-byte mechanical guarantee |
| `tests/test_workbook.py` | The Evidence Export as the organizer's own workbook, cell by cell |

---

## Reproducing Your Submitted Evidence

    uv run python scripts/run_repro.py export
    uv run pytest -q tests/test_evidence_goldens.py

The first command regenerates the submitted rows from frozen checkpoints with **zero model
calls**: label repair, pointer gate and export only. The second asserts the result matches
`tests/golden/EVIDENCE.sha256` byte for byte, so a reviewer can compare what they regenerated
against what we filed.

Every model identity is pinned (exact Ollama blob digests, pinned OpenRouter identifiers,
hash-pinned Python dependencies in `uv.lock` and `requirements.txt`); no stage ever fetches
"latest".

Against the ESCAP RDTII 2.1 Round 1 Database, our derived scores **23/27 agree** across the
three Economies with a checked Corpus. The four disagreements are named and justified cell by
cell in `tests/golden/repro/GT_MATRIX.md`, and a test asserts that the number printed here is
the number the code computes.

Deep-dive references live in `docs/` (Portals, the audit view, the interface, the PostgreSQL
swap, the translation lane), indexed by `docs/README.md`.

---

## Team

| Role | Name | Responsibility |
| :---- | :---- | :---- |
| Technical Lead | Ryan Nurtanio | Pipeline architecture, OCR, interface, packaging |
| Substantive Lead | Zhuo Feng | Legal and policy analysis, output QA |
| Team Lead | Nguyen Kim Hau | Quality assurance, submission coordination |
| Presentation Lead | Ngoc Hong Do (Ellen) | Deck structure and format |
| Substantive Support | Karyn Ho Po Yi | Legal output review |

---

## Licence

Released under the **Apache License 2.0**, as required. See [LICENSE](LICENSE) for the full
text. The shipped dependency set is Apache-2.0-compatible, enforced by a licence gate in CI;
vendored-asset notices are in `THIRD_PARTY_NOTICES.md` and interface bundle notices in
`src/regcompass/ui_dist/THIRD_PARTY_NOTICES.md`.

---

## Release

| | |
| :---- | :---- |
| Release tag | `final-round` |
| Commit SHA | the commit the tag `final-round` points to (shown on the Release page) |
| Docker image | `ghcr.io/ryannurtanio/regcompass-final:final-round` |
| Live URL | https://regcompass.sidequesting.tech |
| Backup copy | Linked from the GitHub Release page of the tag above |

The release tag you record is the version that runs on 15 October. Settings may change on the
day; code may not.

---

## Acknowledgements

Built for the UN Global Hackathon on AI for Digital Trade Regulatory Analysis, organised by
ESCAP and KMITL.
