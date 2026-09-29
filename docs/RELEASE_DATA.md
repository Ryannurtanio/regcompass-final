# Getting the prepared database

The release ships its pre-run Runs as one archive attached to the GitHub Release, next to
the code. Load it and the interface opens with every pre-run Run already in the Runs list,
each Mapping reviewable in the audit view with its source document beside it, and the
Comparison and Evidence Export working at once, with no key and no Run of your own.

The archive is not in the repository or the image: the database alone is about 390 MB and
GitHub refuses any file over 100 MB, and `.dockerignore` keeps `data/` out of the image.

| | |
|---|---|
| File | `regcompass-prepared-<date>.tar.gz`, attached to the Release |
| Checksum | `regcompass-prepared-<date>.tar.gz.sha256`, attached beside it, and the same value in `config/prepared_data.yaml` and the Release notes |
| Download | about 400 MB |
| On disk after loading | about 650 MB: the database plus 63 source documents (one of the 64 the Runs read is left out, see below) |
| Contents | Australia, Malaysia, Singapore, China, Indonesia, India: their Corpus, and the completed Runs on Engine A and Engine B for Pillars 6 and 7 |

The loader checks the SHA-256 of the whole download **before** it unpacks anything. On a
mismatch it deletes the download, changes nothing and says so. It never replaces a database
that already holds Documents or Runs unless you pass `--force`; the empty database the
server creates on its first start is replaced without asking, because there is nothing in
it to lose.

## With Docker (the judge's path)

From a fresh clone, load the data into the volume first, then start the stack:

```bash
git clone https://github.com/<owner>/<repo>.git
cd <repo>
docker compose run --rm --no-deps regcompass-load   # download, verify, unpack into the volume
docker compose up --build                           # then open http://localhost:8000
```

`make docker-load-data` is the same first command. The address and the SHA-256 come from
`config/prepared_data.yaml` in the tagged commit, so no argument is needed. To name them
yourself (copy both from the Release page):

```bash
docker compose run --rm --no-deps regcompass-load regcompass load-data \
  --url https://github.com/<owner>/<repo>/releases/download/<tag>/regcompass-prepared-<date>.tar.gz \
  --sha256 <the 64-character SHA-256>
```

If the stack is already up, stop the app so nothing holds the database open, load, and start
it again:

```bash
docker compose stop regcompass
docker compose run --rm --no-deps regcompass-load
docker compose start regcompass
```

A volume that already holds your own Runs is refused. To replace it anyway:
`docker compose run --rm --no-deps regcompass-load regcompass load-data --force`. To start
again from nothing: `docker compose down -v` removes the volume (and every Run in it).

## Without Docker (a fresh clone)

```bash
git clone https://github.com/<owner>/<repo>.git
cd <repo>
uv sync --extra live
uv run regcompass load-data        # into data/regcompass.db and data/<ECONOMY>/raw/
uv run regcompass serve            # then open http://127.0.0.1:8000
```

`--db` and `--data-dir` choose other places (they must match what `serve` is given). To name
the archive yourself (copy both from the Release page):

```bash
uv run regcompass load-data \
  --url https://github.com/<owner>/<repo>/releases/download/<tag>/regcompass-prepared-<date>.tar.gz \
  --sha256 <the 64-character SHA-256>
```

If `serve` is already running on the same database, stop it with Ctrl+C first, load, and start
it again. Ollama is not needed for any of this: browsing the prepared Runs, the Comparison
and the Evidence Export never call it. It is needed only to start a new Run.

## Checking and unpacking by hand

The archive is laid out exactly like the data folder, so standard tools are enough:

```bash
curl -LO https://github.com/<owner>/<repo>/releases/download/<tag>/regcompass-prepared-<date>.tar.gz
curl -LO https://github.com/<owner>/<repo>/releases/download/<tag>/regcompass-prepared-<date>.tar.gz.sha256
sha256sum -c regcompass-prepared-<date>.tar.gz.sha256   # macOS: shasum -a 256 -c
mkdir -p data && tar -xzf regcompass-prepared-<date>.tar.gz -C data
```

Inside: `regcompass.db`, `prepared-data.json` (Economies, Run ids, row counts, the database's
own SHA-256 and a schema hash), and `<ECONOMY>/raw/<file>` for every Document.

## What is in it, and what was left out

The archive is packed from the working database by `scripts/pack_prepared_data.py`, on a
copy, by these rules:

- **Kept:** the six Economies' Documents with their text, chunks, word boxes and stored text
  streams; Runs that completed on Engine A or Engine B, with their Mappings, Glosses and
  reconciliation groups; completed Discoveries; the crawl manifest rows of the six; the audit
  trail; and the exact source bytes of every kept Document, proven by SHA-256.
- **Left out:** every other Economy (Lao PDR was prepared but is not one of the six);
  interrupted, failed or unfinished Runs; Runs on the keyless `fake` Engine; seeded
  demonstration Documents; Mappings that belong to no Run; audit rows that name anything left
  out; and all Review Decisions, because accepting or rejecting a Mapping is the reviewer's
  act. Stored pages that no Document was read from (Singapore's index pages) are not shipped.
- **One Document the Runs read:** India's Hindi rendering of the Telecommunications Act, 2023
  (`doc_in_H202344`), with its Mappings. Its PDF's text layer lost every Devanagari glyph,
  so its text is not the law's text. India therefore arrives with 4 Documents and the archive
  holds 63, while **Pre-run Coverage** in the README still reports what was run (5 India
  Documents, 64 in all). `prepared-data.json` names it under `dropped`.
- **So every export starts empty:** with no Review Decisions every prepared Run's Evidence Export is empty
  until a reviewer accepts rows. **Accept all N…** on the Evidence screen (after a confirmation) accepts
  every undecided Mapping of the Run at once.
- **Checked before writing:** foreign keys and integrity of the copy, and no absolute home or
  temporary path (`/Users/...`, `/home/...`, `/tmp/...`) anywhere in its text. Any failure
  stops the pack with nothing written.

## For the maintainer: making and attaching the archive

Pack from the working database when no Run or Discovery is writing it. The script only reads
`--db` and `--data-dir`:

```bash
uv run python scripts/pack_prepared_data.py \
  --db data/regcompass.db --data-dir data --out dist/prepared \
  --name regcompass-prepared-<date>
```

It prints the size, the SHA-256 and a per-Economy count, and writes three files to
`dist/prepared/`: the archive, its `.sha256`, and `<name>.manifest.json`. `--drop-run <id>` and
`--drop-document <id>` (both repeatable) leave out a Run or a Document that would otherwise
qualify; `--economies` and `--engines` change the defaults.

Then, in this order:

1. Write the download address and the SHA-256 into `config/prepared_data.yaml` in the commit
   that will be tagged. The address is predictable before the Release exists:
   `https://github.com/<owner>/<repo>/releases/download/<tag>/<name>.tar.gz`.
2. Push the tag. CI runs the checks, pushes the image to `ghcr.io/<owner>/<repo>:<tag>`, and
   opens the GitHub Release with the commit and the image digest in its notes.
3. Attach the archive and its checksum to that Release:

   ```bash
   gh release upload <tag> dist/prepared/<name>.tar.gz dist/prepared/<name>.tar.gz.sha256 \
     --repo <owner>/<repo>
   ```

4. From a machine that has never seen the data, run the Docker commands above and open the
   Runs list.
