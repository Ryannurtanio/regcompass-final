# RegCompass - AI Tool for Digital Trade Regulatory Analysis

UN Global Hackathon on AI for Digital Trade Regulatory Analysis
Team: RegCompass | Round: **Final**
Last updated: 2026-09-29

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
Given an Economy, a Pillar and the Indicators drawn, Discovery first fetches the laws the 2025
RDTII baseline cites for those Indicators from the Economy's official hosts, then adds what the
Portal's crawler finds for that Pillar. The legislation (scanned and image-based PDFs included)
goes into that Economy's Corpus as clean, structured text, with no manual steps. Every Document
says why it was chosen, and every baseline law that could not be fetched is listed with the
reason. Discovery is the only step that touches the internet.

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
China, India, Kazakhstan, Mongolia, Russian Federation, Timor-Leste. Every one of them is configured in
`config/portals.yaml`; adding another is an edit to that file, with no code change. Six of them
were pre-run on both declared Engines for Pillars 6 and 7, from a Corpus fetched from the
official source: **Australia, Malaysia, Singapore, China, Indonesia and India** (see **Pre-run
Coverage**).

**Ready for the live test.** Any of the ten live-test Economies can be mapped today. What
differs is how much of its law Discovery reaches on its own. The honest state on 29 September
2026:

- **Run end to end on both Engines, Pillars 6 and 7, from a Corpus fetched from the official
  source:** Indonesia (7 Documents, live Discovery on 23 September 2026), India (5 Documents:
  4 by live Discovery on 23 September 2026 and the Information Technology Act, 2000 uploaded
  with its official India Code Source URL) and China (3 statutes added by their Source URLs on the
  regulator's site). Their figures are in **Pre-run Coverage**.
- **Corpus fetched by live Discovery, not yet Run:** Lao PDR (37 Documents from the Official
  Gazette on 23 September 2026). It is ready for a Run; we did not spend on pre-running it.
- **What Discovery by Pillar reaches** (the live hour's first step; see **Discovery by Economy,
  Pillar and Indicators**):
  - a Portal crawler plus the baseline laws: Indonesia, India and Lao PDR;
  - the baseline laws plus official addresses seeded per law, with no crawler: China, Thailand,
    Mongolia and the Russian Federation;
  - seeded official addresses only, because no 2025 baseline exists: Viet Nam (two laws from the
    Official Gazette), Kazakhstan (one law, in an unofficial English translation) and Timor-Leste
    (laws from the Ministry of Justice's copy of the Jornal da República, in Portuguese). A draw whose
    Indicators those laws do not serve fetches nothing, and the Discovery report says to upload
    the laws by hand.

  Each seeded address was checked by a live request on 29 September 2026; Discovery itself is
  tested against recorded Portal answers. India's Portal cannot serve its own `robots.txt`, and
  it discovers under a configured policy that says what that silence means for it (see
  **Crawling Politely**).

Whatever Discovery does not reach is added through the interface, by official URL or upload, and
everything after that is the same.

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

    git clone --branch final-round https://github.com/Ryannurtanio/regcompass-final.git regcompass
    cd regcompass

### 2. Set up the environment

Docker, with the `compose` plugin, is the only prerequisite. Nothing is installed on the host:
Python, the OCR engine, all twelve vendored OCR language files, the built interface, the sample
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
  **Run** in the Kind column, and the 6 Discovery records that fetched or added their Corpus,
  marked **Discovery**, or **Discovery (manual add)** where a Document was added by hand.
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
- *A wrong Source URL or title*: in the same Corpus list every title opens the Document's
  **Source URL** in a new tab, and **our copy** opens the file the Corpus stored, so the two can
  be compared. **Edit** on the row changes **Title** and **Source URL** in place (with **Your name
  (optional)**), then **Save**. The Document keeps its id, its text and its Mappings, an address
  that is not http or https is refused, and the next Evidence Export carries the new values.
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
| Start a run and watch progress in plain words | **Start a Run** screen. The **Run panel** box on it takes four steps, **Economy**, **Pillar**, **Indicators** and **Engine**, and **Your Run** beside them sums up the choice with what the last Run on that setup took and cost. Press **Start Run**, read the estimate, then **Yes, start the Run**. The Run view appears below the panel and follows every Document Step by Step: **Read**, **Scan check**, **Split into sections**, **Gate**, **Map and Prove** and **Gloss** (in the code: M1 extract, M2 OCR, M4 chunk, M5 gate, M6 map + M7 verify, M3 gloss), then **Reconcile** once for the whole Run (M8). A Step with nothing to do is shown as skipped rather than going missing. **What the Run has found so far** counts Documents, Sections, Candidates kept by the Gate, Mappings proposed and Mappings proven; **Where the time went** charts each Document's time, Step by Step when you point at it; the header shows the time and what the Run spent at the Engine's declared prices (**Cost so far (our meter)** while it runs, then **Cost (our meter)** and **Engine calls**, with the provider's bill beside it). Click a Document's name for its own drill-down: its **Steps**, its Mappings by Indicator, the **Candidates the Gate kept**, and each quote marked in its source; **Back to the Run** (or `Esc`) returns. **Show raw log** opens the plain log with the code's stage names. The Run view stays on the screen after the Run finishes. With exactly one Pillar chosen the panel also offers **Discover Pillar N** (Discovery for that Pillar and the ticked Indicators, confirmed with **Yes, start Discovery**) and **Discover, then run** (that Discovery, then a Run over the whole Corpus, confirmed with **Yes, discover and run**). The Discovery view says it searched the official legal portals for the Economy, shows each Document with the site it came from, and lists under **Laws not found** each law it could not fetch, each with *Add this law with Add document.* |
| Open the audit view: a result beside the source text it came from | **Evidence** screen, click any row of the **Documents** table (or press Enter). The source PDF opens on the left, the Mapping on the right, with the quote highlighted on the page image. Scanned laws render like any other: the image decoders ship inside the interface, so this works with no internet. A source that is a web page rather than a PDF (China's three statutes) shows its text on the left instead, with the Verbatim Quote marked where it sits and no page number, because a web page has none. The header names the Run on screen (id, Economy, Pillar, Engine), and the record pane prints the **Confidence** composite as a number beside its dots. **How this is scored**, beside the dots, opens the four signals it is computed from (**Meaning match** 45%, **Quote length** 25%, **Specificity** 15%, **Proof attempts** 15%), each with its raw value, weight and contribution and a line on why it counts, adding up to the Confidence shown. The Engine writes the Rationale; the pipeline computes the Confidence. |
| Add a Document to a Corpus by hand | **Start a Run** screen, control **Add document** (also the link beside **Corpus of &lt;Economy&gt;** under **Your Run**): an optional **Law name** (the statute's own name, which the Evidence Export writes into the **Law Name** column), an optional **Source URL** on the upload lane, the file or the URL to fetch, and the **Language**. The control lists that Economy's whole **Corpus** underneath, so an upload is visible the moment it lands, before any Run. Several Documents may share one **Source URL**, which is what a ministry landing page publishing a whole collection needs. |
| Record where an added Document is published | **Start a Run** screen, **Add document**, the **Corpus** list: a Document with no address is marked **no Source URL** and carries a **Set Source URL** field on its row. It edits the Document already in the Corpus, so nothing is fetched and no duplicate is created. |
| See why nothing happens on a fresh install | **Start a Run** screen. An Economy with no Documents says so above the **Run panel** and names the three ways to fill it: button **Discover**, control **Add document**, or `regcompass seed --economy <code>` for the keyless demo. With one Pillar chosen, **Discover Pillar N** is always offered. |
| Follow a row to its official source at the cited article | **Evidence** screen, audit view, link **Open source, page N** on the citation line *section subsection, PDF page N*. It opens the Document's official **Source URL** in a new tab, at the cited page (`#page=N`). A web-page source carries no page number on either. The same link sits on every row of the **Comparison** screen. A Document with no Portal address recorded reads **Open source (local copy)** and opens the stored file the highlight was drawn on. |
| Accept, reject or correct a row | **Evidence** screen, a **Documents** row, then buttons **Accept**, **Reject**, **Flag** (keys `A`, `R`, `F`) with an optional **Note**. To file a Mapping under a different Indicator: button **Correct** (key `C`), then **Correct to** (an Indicator of the Run's own Pillars), a required **Reason** of 1 to 300 characters, an optional **Your name**, and **Save correction**. The Engine's Mapping is never modified: the record reads *Engine proposed X (title), reviewer corrected to Y (title)*, and every decision on a Mapping is kept in an append-only history (`GET /api/reviews/history`). To correct an English rendering: field **English gloss**, then your name in the field beside it and **Mark reviewed**. |
| Work through the rows that need a person first | **Evidence** screen, view **Review queue**: every record of the Run in one list, lowest **Confidence** first, whichever Document it sits in, with a count line (*14 of 60 below 0.60 Confidence, 9 not reviewed yet.*), the control **Order** (**Lowest Confidence first**, the default; **Highest Confidence first**; **Document order**, law by law and page by page), kept in the page address, and two filters, **Not reviewed only** and **Corrected only**. A row opens the same audit view, and `A`, `R`, `F`, `J` and `K` then step in queue order (`C` opens the Correct picker and stays on the row). A row the pipeline could not score reads **not scored**: it comes last in either Confidence order and still counts as below the threshold, because nothing is known about it. |
| See another Economy's evidence without going through Run history | **Evidence** screen, the **Showing** bar above the lists: **Economy**, **Engine** and, where there are several, **Pillars**. It opens on the chosen Economy's newest Run, keeps the choice in the page address so a link can be shared, and, when both Engines have a Run, says *Both Engines have a Run for &lt;Economy&gt;* with **Compare them**. |
| Check a Document against its official source, or fix its Source URL | **Start a Run** screen, control **Add document**, the Corpus list: each title opens the **Source URL** in a new tab and **our copy** opens the stored file the tool read. **Edit** changes **Title** and **Source URL** in place (**Set Source URL** when none is recorded), then **Save**; the Document keeps its id, text and Mappings, and the next Evidence Export carries the new values. |
| Switch the AI engine | **Start a Run** screen, **Run panel** step **4 Engine**: one card per Engine. It opens on the Engine `config/models.yaml` declares as the default (**Engine B**, the open-weight one, marked *The registry default.*), so starting a Run never spends on the commercial Engine by accident. Each card shows the Engine's per-token price and **Key set** or **No key: add one in Settings**. |
| Export to the RDTII schema | **Evidence** screen (or the audit view), header button **Export workbook**. The footer then offers link **Download workbook** (the organizer's `submission.xlsx`) with the CSV and the two JSON files beside it, so the export lands in your own downloads folder and not only in the server's volume. |
| Produce the Engine Comparison file | **Comparison** screen: it opens on the Economy and Pillar of your most recent completed Run. Choose Run A and Run B (the same Economy and Pillar on the two Engines), then the links above the table: **Download sheet (xlsx)**, **Sheet (CSV)**, **Rows (CSV)** and **JSON**. |
| Check how politely the crawler behaves | **Settings** screen, card **Polite crawling** (read-only, from `GET /api/settings/politeness`): each Portal's sites and minimum wait between requests, 1 connection at a time per host, a **robots.txt respected** switch that is locked on, and what happens when a Portal's robots.txt cannot be read. |
| Clear downloads and cache before the clock starts | **Settings** screen, section **Clear downloads and cache**: pick an Economy or Everything, **Preview**, then **Clear now**. |
| See why a Run that finished has nothing to show | A Document whose text carries no section headings is split into numbered passages (*Passage 1*, *Passage 2*, and so on) and the Gate reads them like sections; the raw log says so at *M4 chunk*, and a passage row exports with a note in place of the heading check. A Document with no text to split yields nothing: the **Evidence** screen's banner above the lists names it and says to check the file rather than the Engine, and the same sentence is in the raw log at *M4 chunk*, on the Run Record and in the **Export workbook** refusal. The Run view says why on any Document's row that ends with no Mappings (for example *No numbered headings found, so the whole text was one section and the Gate kept nothing.*). |
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
Indicator. There is no offline toggle to find, because there is no fetching path to turn off: a
Run never fetches, and Discovery happens only when **Discover**, **Discover Pillar N** or
**Discover, then run** is pressed. In the live hour, the first pass is **Discover, then run** on
Engine A; the second pass is a plain **Start Run** on Engine B over the same Corpus, so its
Documents fetched count is 0.

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
| Max requests per second per host | 1 request per `min_interval_seconds`, the per-Portal minimum wait between two requests to the same host. The configured floors are 2 s (Thailand, Lao PDR, India, Viet Nam, Kazakhstan, Mongolia, Russian Federation), 3 s (China, Timor-Leste), 5 s (Malaysia, Indonesia), 6 s (Singapore) and 10 s (Australia); a Portal with no floor of its own waits the default 1.0 s. A Portal's published crawl-delay RAISES the floor and never lowers it. | default `src/regcompass/contracts.py:635`, per Portal `config/portals.yaml`, applied `src/regcompass/discovery.py:564` (the floor) and `src/regcompass/discovery.py:596` (raised by a published crawl-delay); a Discovery by Pillar applies the same floor per host (`src/regcompass/discovery.py:1320`) |
| Parallel requests per host | 1 | `src/regcompass/crawl.py:782` (`httpx.Limits(max_connections=CONNECTIONS_PER_HOST, max_keepalive_connections=CONNECTIONS_PER_HOST)`, with `CONNECTIONS_PER_HOST = 1` at `src/regcompass/contracts.py:602`) |
| robots.txt respected | yes | `src/regcompass/crawl.py:580` (`read_robots_policy`, the one door every fetching lane uses, read for each host a Discovery by Pillar asks), RFC 9309 longest-match rule at `src/regcompass/crawl.py:447` (`RobotsPolicy.allows`) |

Requests go out under an identified user agent naming the project and a contact URL. Documents
already in the Corpus are skipped without a request.

**Hosts never requested.** Three hosts are refused on every path, whatever a baseline row, a
Portal answer, an operator or a redirect names, and a browser subresource included:
`law.go.th`, the Royal Gazette `ratchakitcha.soc.go.th`, and China's national law database
`flk.npc.gov.cn`, whose `robots.txt` forbids every crawler (`NEVER_REQUESTED_HOSTS` in
`src/regcompass/contracts.py`). A redirect off an Economy's official hosts is not followed.

**Plain http for two hosts only.** Port 443 to Russian government hosts is closed on our
network path while port 80 answers, so `pravo.gov.ru` and `kremlin.ru`, and no other host, may
be asked over plain http (`http_hosts` in `config/portals.yaml`), with `robots.txt` read over
the same scheme and a 60-second timeout.

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
Economy + Pillar (+ Indicators)            Economy + Pillar (+ Indicators) + Engine
   |                                                     |
   v                                                     v
DISCOVERY (the only step on the network)         RUN (never touches the network)
  discovery.py  baseline laws first, then          extract.py  canonical text stream per
                the Pillar's crawler seeds;                    Document (the byte authority)
                one cap, one deadline              ocr.py      auto-fires on low-yield pages
  crawl.py      identified agent, robots.txt,                  and garbage text layers:
                one connection, spacing floor,                 tesseract -> RapidOCR ladder
                sha256 manifest                    chunk.py    section-aware chunking; numbered
       |                                                       passages where there are no headings
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
| Portal Crawler | `src/regcompass/discovery.py`, `src/regcompass/crawl.py`, `config/baseline_laws.json` | Reads Portals and the Baseline Law List, records what it fetched and what it refused, with the reason. The only network door. |
| Document Processor | `src/regcompass/extract.py`, `src/regcompass/textnorm.py`, `src/regcompass/ocr.py`, `src/regcompass/chunk.py` | Download, Unicode normalisation (NFC), OCR, structural parsing, section-aware chunking with a numbered-passage fallback |
| Retrieval | `src/regcompass/gate.py`, `src/regcompass/shortlist.py` | Chunking-aware shortlist, embedding, keyword search, ranking |
| Mapper | `src/regcompass/map.py`, `src/regcompass/verify.py`, `src/regcompass/reconcile.py` | Maps a provision to an RDTII Indicator, then proves and reconciles it |
| Interface | `src/regcompass/server.py`, `ui/src/` | Run control, audit view, review, comparison, export |
| Output Writer | `src/regcompass/export.py`, `src/regcompass/workbook.py`, `src/regcompass/traps.py`, `src/regcompass/wording.py`, `src/regcompass/labels.py` | Writes the RDTII schema into the organizer's own workbook, fills its Run Record sheet, cuts 7.1 and 7.2 to one row per Economy and flags rows that fall into a scoring trap |

The pipeline wiring lives in `src/regcompass/pipeline.py` and `src/regcompass/cli.py`; Engine
resolution in `src/regcompass/engines.py`; state in `src/regcompass/storage.py` (SQLite; a
PostgreSQL and pgvector swap ships in `pg.py`, see `docs/POSTGRES.md`).

---

## Swapping the OCR Engine

| Engine | Config value | Notes |
| :---- | :---- | :---- |
| Tesseract (first rung, the default) | vendored language data in `vendor/tessdata/`, chosen per Document by its Language (and, for Malaysia, its Economy) in `src/regcompass/languages.py` | Open source, no key, no network. Apache-2.0. |
| RapidOCR (escalation rung) | automatic on pages below the confidence or dictionary-hit thresholds | Open source, no key, no network. Runs for scripts it has a model for (Latin, Chinese) and is off for the rest. |
| Manual review (final rung) | automatic | A page neither engine could read is flagged for a person with the reason attached, never silently passed as text. |

**None of these is a proprietary service.** The whole OCR path runs offline, which is what makes
the Section 3 declaration hold for OCR as well as for the language model. The same is true of
the Gate embedder (`bge-m3` on a local Ollama) and of translation.

Every OCR-processed page saves an (input PNG, extracted text) evidence pair, so a reviewer can
compute error rates independently.

**A text layer that is not the law's text goes to OCR.** Some PDFs carry a text layer made with
old fonts: it extracts, but as the wrong characters. Before any Document is split, its text layer
is checked (`garbage_text_layer` in `src/regcompass/shortlist.py`): too many unmapped or private-use
glyphs, too few letters, a national-script law whose layer is nearly all Latin letters that are
not English words, a Thai or Lao vowel read as its neighbour, or one line repeated like a
watermark. A layer that fails is set aside and the page images are read by OCR instead, in the
languages of the Document and its Economy.

The verdict travels with the Document rather than with the run that produced it. Whichever lane
wrote the Corpus row, the upload and the Run alike, it records the mean word confidence, the
dictionary hit rate where the script allows one, whether the RapidOCR rung was reached, and
whether a person was asked to look. The export reads those columns for the submission's
`ocr_quality` block, so an escalation is a recorded fact and not something to be inferred from
the extractor's name.

### Vendored language data (artifact table)

Redistributed unmodified from `tessdata_best`
(https://github.com/tesseract-ocr/tessdata_best), Apache-2.0. Every file is pinned here and
`tests/test_vendored_assets.py` hashes the checked-in bytes against this table (and against the
same pins in `THIRD_PARTY_NOTICES.md`). `eng`, `msa` and `lao` were vendored earlier and their
provenance is the digest below; `ind`, `tha` and `rus` were taken from tag `4.1.0` and each was
verified against that tag's GitHub blob SHA on download (16 September 2026); `chi_sim`, `vie`,
`kaz`, `mon` and `hin` were taken from the same tag and verified the same way (29 September
2026), and `por` (Portuguese, read for Timor-Leste's "Other") likewise (30 September 2026).

| File | Language | Bytes | SHA-256 | Source |
|---|---|---|---|---|
| `vendor/tessdata/eng.traineddata` | English | 15,400,601 | `8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba` | vendored earlier |
| `vendor/tessdata/msa.traineddata` | Malay ("Other", Malaysia) | 8,230,552 | `2fa693701d2fb3685cf3e0a64bc1bfb97d66a50f93c07b2ebc0ff360ea62621f` | vendored earlier |
| `vendor/tessdata/lao.traineddata` | Lao | 13,532,551 | `12680f24af571909d69f0dd0151ecdf657a98f000c0ca011c11f8f9dc2bf69b7` | vendored earlier |
| `vendor/tessdata/ind.traineddata` | Bahasa Indonesia | 8,253,606 | `1f6596041ffb4cd5094e5f98764db43cfde04edb8f02b988f90ebc1353ac73b8` | tag 4.1.0, blob SHA verified |
| `vendor/tessdata/tha.traineddata` | Thai | 7,614,571 | `ee8adab6dc69eb8df3d3c8307ae8295471b7bd7d86a06d9267aa8f479b064eac` | tag 4.1.0, blob SHA verified |
| `vendor/tessdata/rus.traineddata` | Russian | 15,301,764 | `b617eb6830ffabaaa795dd87ea7fd251adfe9cf0efe05eb9a2e8128b7728d6b6` | tag 4.1.0, blob SHA verified |
| `vendor/tessdata/chi_sim.traineddata` | Chinese (simplified) | 13,077,423 | `4fef2d1306c8e87616d4d3e4c6c67faf5d44be3342290cf8f2f0f6e3aa7e735b` | tag 4.1.0, blob SHA verified |
| `vendor/tessdata/vie.traineddata` | Vietnamese | 12,435,550 | `b6b49293d95d0b6dbd8780174627e82c75be957b6f4ed9862155540d6b00bb45` | tag 4.1.0, blob SHA verified |
| `vendor/tessdata/kaz.traineddata` | Kazakh | 7,528,853 | `34cbd9204b1ff3cc813d50b29e3c0ae3752bcc201f261a4a393ceca3895aea9d` | tag 4.1.0, blob SHA verified |
| `vendor/tessdata/mon.traineddata` | Mongolian | 8,646,663 | `186dcb2ef79e0dc1ab88da2231926d79070c20176bf7416a19389331f68faf65` | tag 4.1.0, blob SHA verified |
| `vendor/tessdata/hin.traineddata` | Hindi | 11,895,564 | `bd2e65a2184af08a167b0be2439e91fa5edbc4394399ca2f692b843ae26e78d6` | tag 4.1.0, blob SHA verified |
| `vendor/tessdata/por.traineddata` | Portuguese ("Other", Timor-Leste) | 8,159,939 | `711de9dbb8052067bd42f16b9119967f30bada80d57e2ef24f65d09f531adb04` | tag 4.1.0, blob SHA verified |

All twelve files are inside the Docker image and are tested in the container.

**Every language on the organizer's list now has its own data.** A scanned page in Chinese,
Vietnamese, Hindi, Kazakh or Mongolian is read in its own script, each with English as a second
language because official gazettes carry English headers and Latin digits beside the national
script (`languages.TESSERACT_BY_LANGUAGE`); a Timor-Leste page in "Other" is read as Portuguese
(`por+eng`). A page still falls to the next rung, or to manual
review with the reason attached, when its own quality measures are low; the flag is about what
the page yielded, not about a missing file. Adding a language is a file, a row in the table
above, and a row in `languages.TESSERACT_BY_LANGUAGE`.

**RapidOCR is a second reader for Latin script and Chinese.** It has models for those scripts
only, so a Chinese or Latin-script page that tesseract reads with low confidence escalates to it:
a 7-page scan of the Personal Information Protection Law, read on 22 September 2026, gave 7,942
clean characters. For Thai, Lao, Devanagari and Cyrillic scripts it stays off, because re-reading
through a model for another script can only make the text worse. Everything downstream works on
the text either way: the chunker's `article_zh` profile reads `第N条` article headings and `第N章`
chapters, the Gate takes its meaning-only lane for non-English text, and every non-English row
carries a labelled English rendering of its quote.

---

## Supported Economies and Portals

### Discovery by Economy, Pillar and Indicators

This is the live hour's first step. On **Start a Run**, choose the Economy, exactly one Pillar
and the Indicators drawn, then press **Discover Pillar N** (Discovery only) or **Discover, then
run** (Discovery, then a Run over the whole Corpus on the chosen Engine). Over the API it is
`POST /api/discover` with `economy`, `pillar`, `indicators` and `max_documents`. Without a
Pillar, Discovery is the Economy's fixed seed list, exactly as before.

1. **Baseline stage.** The Baseline Law List, `config/baseline_laws.json`, names for each Economy
   and Indicator the laws the RDTII Round 1 and Round 2 Databases cite, with their reference
   addresses: ten Economies (Australia, Malaysia, Singapore, China, India, Indonesia, Lao PDR,
   Mongolia, Russian Federation, Thailand), all twelve Pillars. Viet Nam, Kazakhstan and Timor-Leste
   have no 2025 baseline, and that is recorded rather than invented. The list is built from the
   Databases by `scripts/extract_known_matrix.py`, which writes the KNOWN matrix from the same
   rows, so Discovery and the Discovery Tag read one list. Discovery takes the laws citing the
   drawn Indicators (every Indicator of the Pillar when none are ticked), those cited by more of
   the drawn Indicators first, adds the official addresses seeded per law in
   `config/crawl_seeds.yaml`, and requests only addresses on the Economy's allowed official hosts
   in `config/portals.yaml`. India's laws are also looked up by exact title in India Code's
   search. Each text fetched is checked to be the law named (its numbers, its year, its title
   words) before it is kept; a link that turns out to carry another law is reported, not stored.
2. **Crawler stage.** Where the Portal has a crawler (Australia, Malaysia, Singapore, Indonesia,
   Lao PDR, India), the crawl seeds tagged for the drawn Pillar run next. A seed with no Pillar
   tags counts as Pillars 6 and 7.

**Limits.** One Document cap covers both stages: 12 unless the request names another, which is
clamped to 1 to 30; the interface uses 12. One deadline covers the whole Discovery: 900 seconds
(`discovery_drawn_budget_seconds`, read from `config/pipeline.yaml` when that file sets it), of
which the baseline stage may use at most 600 (`discovery_baseline_budget_seconds`). Each address
gets one attempt of 25 seconds (60 seconds for the Russian Federation's plain-http hosts,
`fetch_timeout_seconds` in `config/portals.yaml`, and 60 seconds for Timor-Leste's gazette issues), a host that does not answer is not asked again
in that Discovery, and the baseline stage tries at most twice the cap in addresses. Every politeness rule under
**Crawling Politely** applies, with `robots.txt` read for each host asked.

**What the operator sees.** The Discovery view says what it found, not how: each Document with
the site it came from and whether it was added, and under **Laws not found** every law for the
draw that was not fetched, each with *Add this law with Add document.* When nothing at all was
fetched it says *No laws could be fetched for this search. Add them with Add document.* The
workings stay on the record for audit. The Discovery record keeps why each Document came in
(*baseline 6.1, 6.2*, *official source list 7.1* or *portal crawler*) and why each law was not
fetched: the address is not on the Economy's official hosts, the host could not be reached, the
page has no law text, the Document limit or the time limit was reached, the baseline gives no
address, the host's `robots.txt` refuses it, the address redirected off the official hosts, the
title search found no exact match, the link carries a different law, or the baseline gives only
the Portal's summary page for the law, not its text. The progress log, closed behind **Show the
progress log**, prints the same reasons. A Document already in the Corpus is not fetched again;
after **Clear downloads and cache** everything is fetched anew.

**Official sources added for the Economies with no crawler** (seeded in `config/crawl_seeds.yaml`,
each checked by a live request on 29 September 2026):

| Economy | Host | What is seeded |
| :---- | :---- | :---- |
| Thailand | `mdes.go.th` (Ministry of Digital Economy and Society) | Personal Data Protection Act B.E. 2562 (2019), Electronic Transactions Act B.E. 2544 (2001), Computer-related Crime Act B.E. 2550 (2007): the government's unofficial English translations, so their Language is English |
| Viet Nam | `congbao.chinhphu.vn`, `congbaocdn.chinhphu.vn` (Official Gazette) | At least one law for each of the twelve Pillars (18 addresses), among them the Law on Personal Data Protection No. 91/2025/QH15 with Decree No. 356/2025/ND-CP and the Law on Cybersecurity No. 116/2025/QH15; PDFs with a real Vietnamese text layer, as promulgated, or the consolidated text where the National Assembly Office published one. Decree No. 13/2023/ND-CP and Law No. 24/2018/QH14 are no longer in force and are not seeded |
| Mongolia | `legalinfo.mn` | Law on Personal Data Protection; Law on Cybersecurity (whole law pages, server HTML) |
| Russian Federation | `kremlin.ru`, `pravo.gov.ru` (plain http); `eec.eaeunion.org` (Eurasian Economic Commission, https) | Federal Law No. 152-FZ On Personal Data (kremlin.ru print page and pravo.gov.ru); Federal Law No. 149-FZ On Information, Information Technologies and the Protection of Information (pravo.gov.ru); kremlin.ru print pages for nine more federal laws the 2025 baseline cites; the Eurasian Economic Union acts that bind it (Treaty Annexes 8 and 9 and Section X, Board Decision No. 30 with its Annex 9, the Customs Code) |
| Kazakhstan | `natlex.ilo.org` (ILO NATLEX, intergovernmental repository of official texts); `eec.eaeunion.org` (Eurasian Economic Commission) | Law No. 94-V On Personal Data and their Protection (the Ministry of Justice's unofficial English translation, amended to 30 December 2021) and the Entrepreneurial Code No. 375-V, both in English, so their Language is English; the Eurasian Economic Union acts that bind it (Treaty Annex 8, Section XXII with Annex 25, Section X and Annex 9, Board Decision No. 30 with its Annex 9, the Customs Code) |
| Timor-Leste | `www.mj.gov.tl`, `mj.gov.tl` (the Ministry of Justice's copy of the Jornal da República, the official gazette); `timor-leste.gov.tl` (Government); `anc.tl` (Autoridade Nacional de Comunicações, the telecommunications regulator) | At least one law for each of the twelve Pillars (21 addresses, checked on 30 September 2026), among them Decree-Law No. 10/2024 on Trade Defence Measures and No. 12/2024 on Electronic Commerce, the Public Procurement Code (Decree-Law No. 1/2025), the Private Investment Law (Law No. 15/2017), the Copyright Code (Law No. 14/2022), the telecommunications Decree-Law No. 15/2012, the Media Law (Law No. 5/2014), the Customs Code (Decree-Law No. 14/2017), the National Payments System (Decree-Law No. 17/2015) and the Constitution (Article 38, personal data); Portuguese gazette issues with a text layer, each title naming the issue and the file pages its law sits on, and the regulator's English guidelines. No law is in force for patents, trade secrets, personal data protection or cybersecurity |

China, Thailand, Mongolia and the Russian Federation also reach the baseline laws whose addresses
sit on their official hosts. A seeded law serves only the Indicators it is listed for; Viet Nam,
Kazakhstan and Timor-Leste have nothing else to fetch.

### Portals

| Economy | Official portal | Language | Run end to end? | Notes |
| :---- | :---- | :---- | :---- | :---- |
| Australia | `www.legislation.gov.au` | English | **Yes.** Corpus fetched and checked; Runs and Exports green. | Genuine JavaScript application, so Discovery needs the headless browser. The image carries it, so this Portal runs in the container as well as on the host. |
| Malaysia | `lom.agc.gov.my` | English (Malay editions) | **Yes** from the held Corpus; Discovery is broken upstream. | Since 16 Sep 2026 the Portal's search answers with an encrypted payload instead of a result set, so Discovery finds nothing and says so. The printed host `lor.agc.gov.my` is dead; `lom.agc.gov.my` is live. Malay is not one of the organizer's eleven values, so a Malay Document exports as "Other". |
| Singapore | `sso.agc.gov.sg` | English | **Yes.** Corpus fetched and checked; Runs and Exports green. | Escalation ladder after the WAF refuses the identified agent. Document is the whole-act consolidation PDF. |
| Indonesia | `peraturan.bpk.go.id` | Bahasa Indonesia, English | **Yes.** Live Discovery on 23 Sep 2026 fetched 7 Documents; pre-run on both Engines, Pillars 6 and 7. | The Audit Board's national regulation database. Its rules permit every route we take and publish no crawl-delay, while its edge answers our identified agent with 403, so it takes the same escalation ladder Singapore takes. `peraturan.go.id` is whitelisted but unreachable from our network. |
| Thailand | `searchlaw.ocs.go.th`; `mdes.go.th` | Thai, English | **Not yet.** Discovery by Pillar fetches the baseline laws on official hosts and three seeded laws from `mdes.go.th`; no crawler. Add by URL or upload for the rest. | The host printed in our own plan, `www.krisdika.go.th`, is a dead placeholder; the live Council of State law library is `searchlaw.ocs.go.th`, and its certificate verifies cleanly, so the planned certificate pinning was not needed and was not added. Its browse lane answers us and names the statutes we want, but all three of its search services fail with the same error and both of its law-text services reject the identifier the browse lane gives, so there is no path from a seed to a Thai statute today. Verbatim answers in `tests/fixtures/portals/th/`. Thai OCR data is vendored, so an uploaded Thai scan reads normally. Since 29 Sep 2026 the Ministry of Digital Economy and Society's law pages answer with the law's PDF; it carries only the laws in its own remit (digital economy, electronic transactions, cybersecurity, personal data, computer crime). `www.law.go.th` and the Royal Gazette are never requested. |
| Lao PDR | `laoofficialgazette.gov.la` | Lao, English | **Discovery yes, Runs not yet.** Live Discovery on 23 Sep 2026 fetched 37 Documents; not pre-run. | Server-rendered gazette. `/robots.txt` answers 200 with the homepage (a soft 404), so no rules are published and our own 2 s floor stands. The Lao PDF is the Document; an English rendering beside it is recorded in Notes and is never the quote source. |
| Viet Nam | `vbpl.vn`; `congbao.chinhphu.vn` | Vietnamese, English | **Not yet.** No baseline; Discovery by Pillar fetches seeded laws from the Official Gazette, at least one for each Pillar. Add by URL or upload for the rest. | The Portal publishes real rules and a sitemap, but the sitemap is four navigation pages and the document list exists only behind `/api/`, which those rules forbid. A headless browser would make the forbidden calls itself, so we declined it. The Official Gazette (`congbao.chinhphu.vn`) serves each document page with a link to the law's PDF on `congbaocdn.chinhphu.vn`, with a real text layer, and its rules allow it; `vanban.chinhphu.vn` is not used, because its files are scanned images. Vietnamese OCR data is vendored. |
| China | `www.cac.gov.cn` | Chinese | **Yes**, from three statutes added by Source URL. Pre-run on both Engines, Pillars 6 and 7. Discovery by Pillar fetches the baseline laws on official hosts; no crawler. | The Cyberspace Administration of China, the regulator that enforces the Personal Information Protection Law, the Data Security Law and the Cybersecurity Law, republishes the National People's Congress text in full and its rules permit the law pages, so each statute is added by its Source URL on that host. The national law database `flk.npc.gov.cn` forbids any automated collection in its `robots.txt`, so it is never whitelisted or requested; `www.npc.gov.cn` refused HTTPS and stalled over HTTP. A scan uploaded by hand also works: RapidOCR reads Chinese, the chunker reads `第N条` articles, and the rows ship with Language of Source `Chinese` and a labelled English rendering; Chinese OCR data is vendored for the first rung too. |
| India | `indiacode.gov.in` | English, Hindi | **Yes.** Live Discovery on 23 Sep 2026 fetched 4 Documents; the Information Technology Act, 2000 was uploaded with its official India Code Source URL, because Discovery's title search does not reach it; pre-run on 5 Documents, both Engines, Pillars 6 and 7. | The Portal moved from the printed host and now exposes a read-only DSpace REST interface, which the adapter reads. Its `robots.txt` answered HTTP 500 then 502 on 16 Sep 2026, so it can show no rules; India carries `robots_unavailable_policy: proceed` in `config/portals.yaml` (operator decision, 17 Sep 2026), so Discovery and the add-by-URL lane go ahead at the 2 s floor and every record says the status and the policy. Delete that line to return to the default refusal. Hindi OCR data is vendored. |
| Kazakhstan | `adilet.zan.kz`; `natlex.ilo.org`; `eec.eaeunion.org` | Russian, Kazakh, English | **Not yet.** No baseline; Discovery by Pillar fetches two seeded laws from ILO NATLEX, in English, and the Eurasian Economic Union acts from the Eurasian Economic Commission. Add by URL or upload for the rest. | The act route answers a small shell whose own comment says the full text is withheld from non-browser clients on purpose, so that the system is cited rather than drained. A headless browser would render around that ask, so we do not. Since 29 Sep 2026 two laws are seeded from ILO NATLEX, whose download addresses its rules allow, and the Union acts from `eec.eaeunion.org`, whose rules allow its `/upload/` files; its detail pages sit behind a browser check and are never browsed. Russian and Kazakh OCR data are vendored. |
| Mongolia | `legalinfo.mn` | Mongolian, English | **Not yet.** Discovery by Pillar fetches the baseline laws on official hosts and two seeded law pages; no crawler. Add by URL or upload for the rest. | Every act list, search included, arrives through an undocumented POST endpoint returning pre-rendered markup; the page advertised as an API reference is an article, not an API. A law page, `/mn/detail?lawId=N`, is whole server HTML, so laws are seeded by that address. Mongolian OCR data is vendored. |
| Russian Federation | `pravo.gov.ru`, `kremlin.ru` (plain http); `eec.eaeunion.org` | Russian | **Not yet.** Discovery by Pillar fetches the baseline laws on those hosts and the seeded laws, the Eurasian Economic Union acts among them; no crawler. Add by URL or upload for the rest. | Port 443 to Russian government hosts is blocked on our network path; port 80 answers. `publication.pravo.gov.ru` publishes image-only scanned amendments, not consolidated law, so it is not used. `kremlin.ru` (the President's acts bank, `/acts/bank/N/print` is the whole current text) and `pravo.gov.ru` (the Official Internet Portal of Legal Information) are whitelisted and asked over plain http only, with a 60-second timeout; `eec.eaeunion.org`, the Eurasian Economic Commission, answers over https and is asked that way. Russian OCR data is vendored. |
| Timor-Leste | `www.mj.gov.tl` (Jornal da República); `timor-leste.gov.tl`; `anc.tl` | Other (Portuguese), English | **Not yet.** No baseline; Discovery by Pillar fetches seeded laws from the Ministry of Justice's copy of the Jornal da República, at least one for each Pillar. Add by URL or upload for the rest. | The gazette's own host, `www.jornal.gov.tl`, does not resolve and the National Parliament's site does not answer, so laws are seeded at the Ministry of Justice's gazette address, one PDF per issue with a text layer; its `robots.txt` allows those files and asks for a 10-second crawl-delay, which Discovery honours. Portuguese is not one of the organizer's eleven values, so a Timor-Leste Document exports as "Other"; Portuguese OCR data is vendored and read for it, and the splitter reads its `Artigo N.º` headings. |

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
  layer extracts as unreadable characters and too few pages fell below the OCR threshold to
  trigger OCR, so it yielded no Mappings. The text-layer check added since (see **Swapping the OCR
  Engine**) recognises that layer and sends the Document to OCR, where Hindi data is now vendored;
  the prepared Runs predate it, and it has not been re-run. The Consumer Protection Act 2019, which we looked for,
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
| 6 | article | Required | Exact article and paragraph at the quote's position, as the Economy drafts it (`Art. 5` for civil-law statutes, `s. 26` for common-law ones); for 7.1 and 7.2 the law as a whole. Written as `Article / Section`. |
| 7 | discovery_tag | Required | KNOWN = the 2025 RDTII baseline cites this law for this Indicator at this provision, or cites the law with no article; NEW = anything else. The law is recognised by its title in any script or by a Source URL the baseline gives for that law alone. Written as `Discovery Tag`. |
| 8 | location_reference | Optional | PDF page number, or HTML anchor / section path. Written as `Location Reference`. |
| 9 | verbatim_snippet | Required | Exact quoted text, byte-verified, no paraphrasing. Written as `Verbatim Snippet`. |
| 10 | mapping_rationale | Optional | Max 300 characters: why this provision maps to this Indicator, in English, naming the RDTII scoring criterion it meets (*RDTII criterion N (score X)*). Written as `Mapping Rationale`. |
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

The template's example rows 7 and 8 are deleted, as its Instructions ask, and every formula,
the autofilter and the Coverage Matrix bounds move up with them, so rows land in Output Data from
row 7 and never past row 107, at most 101 provision rows; over the cap the Economies take turns
by Confidence and the Export says how many rows were left out. "No provision found" absence rows
are CSV-only, because each workbook row counts as a provision in the organizer's Coverage Matrix.

**7.1 and 7.2: one economy-level row each.** The organizers score these once per Economy (does a
data-protection framework, a cybersecurity framework, exist?), and a row per provision scores
zero. So an Export from the database keeps at most one row per Economy for each: the framework
law as a whole in Article / Section, quoting its scope or purpose clause where one was mapped,
with the quoted provision named in Notes. The law is chosen from the verified Mappings: one whose
title names a data-protection law (7.1) or a cybersecurity law (7.2), then one whose opening
title block does, then a law the 2025 baseline cites for that Indicator, then one a reviewer
corrected to it; a principal Act outranks an amending act or a regulation. No fitting law, no
row, and `supplementary.json` says why.

**The Run Record sheet.** The same Export fills the organizers' Run Record sheet from the Run
Records, never by hand. Block 1 is one row per pass: Engine (provider and model), start and end
time, elapsed minutes and cost. The first pass (Engine A) covers its Run plus every Discovery and
add of the hour, so its time and cost include the fetching; the second pass is its own Run
alone. Block 2 (rows 12 to 56) lists each Document downloaded in the hour, once, as *Engine A
pass*; the second pass downloads nothing, so it logs nothing. A Document added from a file was
not downloaded: it is named in the note row and never counted as fetched. A stopped or failed Run
does not close a pass.

**Rows to check before submitting.** A row that falls into one of the Indicator Reference's
scoring traps is kept but flagged in Notes with *Check before submitting:*: a quote from a draft,
a repealed or an amending instrument, an Elucidation, a 7.3 row with no stated duration, or a 6.1
row whose transfer is allowed on a condition (a 6.4 shape). A row from a Document split into
numbered passages carries a note that it has no heading to check.

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
chunk (the word-for-word check reads subsection numbering as the law writes it: `（一）`, `(๑)`,
`(а)`, `a)` and `1.` included); the Source URL host on the Portal whitelist, or exempted with its Notes disclosure
present; the Source URL live; a repealed instrument flagged in Notes; no leftover template
example content; the Mapping Rationale within 300 characters; Confidence inside 0.00 to 1.00; a
non-English Verbatim Quote carrying a non-empty English rendering, labelled non-authoritative
unless a named reviewer approved it; the section label matching the Document's own heading on
controlling rows (a numbered passage is noted rather than checked); and no duplicate
provision-Indicator row. A red battery ships nothing.

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

- **OCR in every organizer language, proven on few scans.** Data for all twelve vendored
  languages is in the image, but only the Lao and Chinese lanes have been read on real scanned
  laws here (Chinese by the RapidOCR rung); the Chinese, Vietnamese, Kazakh, Mongolian and Hindi data were added on
  29 September 2026 and are tested for presence and routing, not yet for quality on a real scan.
- **OCR fragility on scans generally.** The OCR ladder saves an evidence pair per page and flags
  what it could not read, but a badly scanned page produces a page that is flagged, not a page
  that is mapped. A measured character error rate is reported as null rather than guessed,
  because it needs a hand-checked reference.
- **What Discovery by Pillar cannot reach.** Seven of the thirteen Economies have no Portal crawler,
  for reasons recorded per Portal: the national law database forbids automated collection
  (China), rules that forbid the only route to the document list (Viet Nam, Kazakhstan), no
  published route at all (Mongolia), a gazette with no crawler written for it yet (Timor-Leste,
  one PDF per issue), a Portal that serves only scanned amendments (Russian
  Federation), and a Portal whose search and law-text services both fail for its own
  application's requests (Thailand). Discovery by Pillar reaches them through the baseline laws
  whose addresses sit on their official hosts and through the laws seeded per Economy. A
  baseline link can be dead, point off the official hosts, or carry another law; each is
  reported with its reason and not stored. Viet Nam, Kazakhstan and Timor-Leste have no 2025
  baseline, so a draw outside their seeded laws fetches nothing, and the operator uploads the laws
  by hand.
  The Thai and Kazakh seeded texts are unofficial English translations published by the
  government (Thailand) and on ILO NATLEX (Kazakhstan), and their rows say so in Language of
  Source. India's Portal cannot serve its own `robots.txt`, and it discovers under the
  configured policy that says what that silence means for it, disclosed on every record.
  Malaysia's Portal search broke upstream on 16 September 2026. In every one of these cases a
  reviewer can still upload the Document or add it by URL, and everything after that is
  unchanged.
- **Documents with no headings the splitter knows.** Such a Document is split into numbered
  passages of about 1,000 characters (*Passage N*) so that it still yields evidence; a row then
  cites the passage, not an article, and says so in Notes. The splitter reads the drafting
  styles of the Round 1 Economies and the article headings of Indonesian (*Pasal*), Chinese
  (`第N条`), Lao, Thai, Russian, Vietnamese, Mongolian, Kazakh and English (*Article N*) laws; a
  law drafted any other way falls back to passages.
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

**2,775 tests**, offline and keyless. Tests that would spend money skip unless
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
| `tests/test_add_correctness.py` | Adding a Document, and the lists and files built from what is added, tell the truth |
| `tests/test_add_document.py` | A reviewer adds a Document by upload or by Source URL |
| `tests/test_audit.py` | The audit view: bundle loading, quote location, highlight rectangles, the accepted-only export gate |
| `tests/test_baseline_laws.py` | The Baseline Law List and the Discovery Tag for every baseline Economy, with law names matched in any script |
| `tests/test_call_deadline.py` | A hung Engine call cannot stall a Run: every call carries a wall-clock deadline |
| `tests/test_chunk.py` | Deterministic section splitting across every drafting style the splitter reads (Mongolian, Kazakh, Lao and English *Article N* included), Unicode normalisation, the numbered-passage fallback, and the coverage partition invariant |
| `tests/test_classify.py` | The closed-menu classification schema and every branch of the scoring rubric |
| `tests/test_clear.py` | Clearing the downloads and every cache: the scope, the data-root fence, the endpoint, the command line, and a second pass before and after |
| `tests/test_cli_discover.py` | The two command-line lanes: `discover` fills a Corpus, `run` reads it |
| `tests/test_cli_seed.py` | `regcompass seed`, the offline step in front of the keyless demo |
| `tests/test_compare.py` | The Comparison: two Runs on one Economy and Pillar, side by side per Indicator |
| `tests/test_config.py` | Every committed config file loads through its contract model |
| `tests/test_contracts.py` | Seam models round-trip unchanged, and invariants fail at construction time |
| `tests/test_convert_run_log.py` | The first replayable Run, rebuilt from its log into the event file the app records |
| `tests/test_corpus_edit_and_evidence_runs.py` | Editing a Document's Source URL and title in place, and the list of Runs the Evidence screen's Showing bar chooses from |
| `tests/test_corpus_paths.py` | A Corpus row survives the data folder moving |
| `tests/test_corpus_run.py` | A Run reads the Corpus and never fetches, proved with a network guard |
| `tests/test_correction_export.py` | A corrected Mapping in the Evidence Export: the corrected Indicator, the reviewer's reason, the override in Notes |
| `tests/test_crawl.py` | Manifest resume, SHA-256 dedupe, failed URLs recorded not retried blindly |
| `tests/test_discovery.py` | Discovery against recorded Portal answers, including "no request was made" |
| `tests/test_discovery_by_pillar.py` | Discovery by Economy, Pillar and Indicators: baseline laws first, then the crawler, the cap, the deadline, the reasons, the hosts never requested |
| `tests/test_discovery_events.py` | Discovery's typed events: what it found, fetched, added and skipped, and why |
| `tests/test_docker.py` | The image, the compose file and the health check a judge's one command depends on |
| `tests/test_document_ids.py` | A Document id is unique per bytes, whatever script the file name is in |
| `tests/test_e2e.py` | The end-to-end lane, the single-PDF lane and the off-corpus export fallback |
| `tests/test_engine_comparison.py` | The organizers' Engine Comparison sheet: every provision either Engine cited, paired, with a summary per pass |
| `tests/test_engines.py` | The Engine registry: one named Engine drives every model-calling stage |
| `tests/test_evidence_goldens.py` | Every shipped evidence artifact is pinned by exact bytes |
| `tests/test_export.py` | The column contract, the gate battery and the goldens |
| `tests/test_export_by_url_rows.py` | Rows built on a Document added by URL (an HTML page) read right |
| `tests/test_export_duplicate_rows.py` | A real Run always exports, because duplicate provision rows collapse first |
| `tests/test_export_fixture_notes.py` | A seeded Corpus discloses itself in the Export |
| `tests/test_export_polish.py` | Articles for civil-law statutes, the RDTII's own words in the rationale, and the scoring-trap flags |
| `tests/test_export_refusal_words.py` | The Evidence Export's refusals, in words a reviewer can act on |
| `tests/test_export_run_indicators.py` | An Evidence Export answers only for the Indicators its Run searched: the absence rows and the screened count follow the Run's Indicator list |
| `tests/test_extract.py` | Page and word slice invariants, determinism, coverage against an independent baseline |
| `tests/test_extraction_reuse.py` | A Run reads a Document's text once and reuses it: the extraction key, the cache hit and miss, the Run Record's reused or extracted map |
| `tests/test_gate.py` | The two-tier Gate over real chunks, with recall ground truth from the ESCAP database |
| `tests/test_glosses.py` | English renderings drafted by the selected Engine, labelled and counted |
| `tests/test_gt_matrix_artifact.py` | The frozen 27-cell ground-truth comparison covers every cell and justifies every mismatch |
| `tests/test_gt_scores.py` | The published agreement number is the one the code computes |
| `tests/test_known_matrix_extract.py` | Section-reference parsing behind the NEW and KNOWN tags |
| `tests/test_labels.py` | Provision labels in each Economy's own drafting word |
| `tests/test_languages.py` | Which OCR data a Language asks for, which Gate tier can read it, and which text layers are sent to OCR |
| `tests/test_lao_portal.py` | Lao PDR's gazette, replayed from recorded bytes |
| `tests/test_live_test_robustness.py` | The first upload on a fresh install, a Document with no structure, a Run left behind by a dead process, and an OCR escalation on the row |
| `tests/test_map.py` | The Engine selects a quote per (chunk, Indicator) pair, and the code anchors or rejects it |
| `tests/test_non_english_lane.py` | OCR in the Document's Language and the meaning-only Gate, end to end |
| `tests/test_ocr.py` | Both scanned fixtures with quality proxies, the escalation ladder and the manual-review flag |
| `tests/test_official_sources.py` | The official sources for the Economies with no Portal crawler: allowed hosts, seeded addresses, plain http for two hosts |
| `tests/test_paid_optin.py` | Money never leaks into a bare test run |
| `tests/test_parallel_mapping.py` | Mapping calls with bounded concurrency: record order equals pair order, byte-identical export, rate-limit backoff, a hard failure keeps the other records |
| `tests/test_pg.py` | PostgreSQL and pgvector parity against the shipped seams (skips without a DSN) |
| `tests/test_pipeline.py` | The judged path: `regcompass run` and `regcompass export` wiring, offline |
| `tests/test_politeness_settings.py` | The read-only politeness endpoint behind the Settings card Polite crawling |
| `tests/test_portal_id.py` | Indonesia's Portal, Discovery and the escalation that makes it possible |
| `tests/test_pre_run.py` | The pre-run job: resumable skip of completed Runs, the spend ceiling, the per-Run stop, the ledger and the coverage report |
| `tests/test_pre_run_parallel.py` | The pre-run job with several Runs going at once |
| `tests/test_prepared_data.py` | The prepared database a release ships, packed, verified and loaded |
| `tests/test_quote_page.py` | The page a Mapping cites is the page its Verbatim Quote sits on |
| `tests/test_reconcile.py` | The legal-hierarchy ladder enforced in code, not by the model |
| `tests/test_registry.py` | Economies and Indicators are data: any Economy, any Pillar, no code change |
| `tests/test_rehearse_discovery.py` | The Discovery rehearsal script: one Economy and Pillar end to end over recorded answers, the guard that makes an Engine call impossible, resume, and the table |
| `tests/test_release_workflow.py` | What a release tag publishes, and how the prepared database reaches the container |
| `tests/test_repro_checkpoints.py` | Resume safety for the reproduction script's checkpoints |
| `tests/test_review_corrections.py` | Correct: a reviewer's override of the Indicator an Engine chose, with the Engine's Mapping kept |
| `tests/test_reviews.py` | Review Decisions survive a refresh, a restart and a migration |
| `tests/test_run_drilldown.py` | Clicking deeper into a Run: one Document, one Candidate, one Mapping |
| `tests/test_run_events.py` | The Run's typed events: on the live stream, in a file per Run, on the record |
| `tests/test_run_progress.py` | The Run's progress: the free-text lines and the Step hook beside them |
| `tests/test_run_records.py` | Run Records: tokens and cost proved against a temporary registry |
| `tests/test_run_replay.py` | Watch a Run again: a recorded Run's events streamed back at a chosen speed |
| `tests/test_run_scoped_mappings.py` | A Run owns its Mappings, and a Run can be narrowed to Indicators |
| `tests/test_section_label_repair.py` | Quote-anchored section-label repair and the headings the chunker misses |
| `tests/test_seed.py` | A Corpus a judge can Run against with no network |
| `tests/test_server.py` | The one server: Start a Run API, Run history, Settings key, audit view, export |
| `tests/test_server_auth.py` | The optional login on a hosted copy |
| `tests/test_sheet_rows.py` | Deleting the template's example rows the way a spreadsheet application does, formulas moved up |
| `tests/test_shortlist.py` | The whole-document shortlist ranker, with recall and precision measured |
| `tests/test_source_links.py` | Following a Mapping row to its official source: the page fragment, the local-copy fallback, the fields the interface reads |
| `tests/test_storage.py` | Schema application, audit logging and the embedding round-trip |
| `tests/test_storage_wal_switch.py` | Several Runs opening one database at the same moment |
| `tests/test_thailand_portal.py` | Thailand's Council of State library, replayed from recorded bytes, and why it is not crawled |
| `tests/test_translate.py` | The translation lane, its default-off flag and its degenerate-output guard |
| `tests/test_traps.py` | The Indicator Reference's scoring traps, flagged on every exported row |
| `tests/test_upload_collisions.py` | Several Documents may share one Source URL, and a Run killed the moment it starts still leaves a record |
| `tests/test_vendored_assets.py` | The vendored OCR language data is exactly what this README says it is |
| `tests/test_verify.py` | The byte-for-byte mechanical guarantee, and subsection numbering in non-English styles |
| `tests/test_vps_deploy.py` | The hosted-server override and its Caddyfile: login required, no way around it |
| `tests/test_wording.py` | Rationales read "RDTII criterion N (score X)" instead of the prompt's "rung N" |
| `tests/test_workbook.py` | The Evidence Export as the organizer's own workbook, cell by cell: rows 7 to 107, the Run Record sheet, the 7.1 and 7.2 economy-level rows |

---

## Reproducing Your Submitted Evidence

### The final-round evidence workbook

The evidence workbook submitted with this release is built from the prepared database (see
**Getting the prepared database**) by this package's own export, with no model call. Every row's
Notes name the Engine, the model and the Run it came from, so any row can be traced to one Run
and regenerated from it:

1. Load the prepared database: `docker compose run --rm --no-deps regcompass-load`, or
   `uv run regcompass load-data` on the host.
2. List the Runs: `uv run regcompass runs --limit 50`, or the **Run history** screen. The
   workbook draws on the 24 pre-run Runs (six Economies, Pillars 6 and 7, both Engines); its
   rows come from the Engine A Runs.
3. Export the Run a row names, from the database alone:

       uv run regcompass export --run-id <run_id> --out out/<run_id>

   or, on the Docker path,

       docker compose run --rm --no-deps regcompass-demo regcompass export --run-id <run_id> --out out/<run_id>

   It writes that Run's `submission.xlsx`, `submission.csv` and the two JSON files after the full
   battery passes: the same code the interface's **Export workbook** runs. The interface ships
   only the rows a reviewer accepted or corrected, and the prepared database carries no Review
   Decisions, so the command line is the route that shows every verified row.

Every cell of a workbook row except Notes (columns A to L and N) holds the value the product export writes for that Run.
What the submission builder adds on top is a separate script of ours, kept outside this
repository, which imports this package as a library and changes none of its code:

- **Selection across Runs.** It exports all 24 Runs with the per-Run cap of 101 rows switched
  off, then keeps at most 101 rows in all, from the Engine A Runs: first the best row for every
  (Economy, Indicator) that has one (a principal Act before an amending act, the Run's
  controlling evidence first, then Confidence), then further rows with the Economies taking
  turns, best Confidence first, never 7.1 or 7.2, never an amending act and never a superseded
  row.
- **Stricter trap handling.** A row the product export keeps with a *Check before submitting*
  note because it is an Elucidation, a 7.3 row with no stated duration or a conditional 6.1 row
  is left out of the submission instead.
- **Notes.** It appends the provenance (Engine, model, Run id) and, for a non-English quote, the
  Run's English rendering.
- **Presentation.** Wider evidence columns, row heights that show each cell's whole text, and
  the cached values of the template's own formulas, so the Coverage Matrix shows its counts
  before a spreadsheet recalculates. No value outside Notes is changed.
- **Checks.** It checks every template rule mechanically and writes a check report beside the
  workbook.

### The Round 1 evidence, from frozen checkpoints

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
| Release tag | `final-round` on https://github.com/Ryannurtanio/regcompass-final |
| Commit SHA | the commit the tag `final-round` points to, printed on its GitHub Release page |
| Docker image | `ghcr.io/ryannurtanio/regcompass-final:final-round`, with its digest on the same Release page |
| Live URL | https://regcompass.sidequesting.tech (sign-in page; the judges' login is given in the submission form) |
| Backup copy | Linked from the GitHub Release page of the tag above |

The release tag you record is the version that runs on 15 October. Settings may change on the
day; code may not.

---

## Acknowledgements

Built for the UN Global Hackathon on AI for Digital Trade Regulatory Analysis, organised by
ESCAP and KMITL.
