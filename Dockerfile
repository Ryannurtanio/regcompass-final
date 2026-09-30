# RegCompass. CPU-only. Serves the one interface (Run panel, Runs, audit view,
# Settings) and, on a Run, drives the pipeline against an Ollama service for
# the Gate embeddings (see docker-compose.yml).
FROM python:3.12-slim

# Tesseract is the OCR ENGINE binary for the M2 escalation lane. The vendored
# tessdata under vendor/ is language data, not the engine, so the package must
# be installed here (tesseract-ocr-eng covers the born-digital demo corpus;
# the vendored traineddata files under vendor/tessdata, picked per Document
# from its Language, are what the code actually reads).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# Fully-resolved, hash-pinned dependency set (uv export --all-extras --no-dev).
# Copied on its own first so the slow install layer caches across source edits.
# --require-hashes verifies every wheel; the file lists all transitive deps.
COPY requirements.txt ./
RUN pip install --require-hashes --no-deps -r requirements.txt

# The headless browser. Discovery for a JavaScript Portal (Australia's, and the
# last rung of Singapore's escalation ladder) calls chromium.launch(headless=True)
# in src/regcompass/crawl.py, so without a browser here that Portal would be a
# host-only path. Chromium alone: firefox and webkit are never launched.
# --only-shell downloads just the headless shell, which is the binary a
# headless=True launch runs, and leaves out the full browser, several hundred
# megabytes more, that nothing here would ever start. --with-deps adds the system libraries the shell needs,
# through apt, so this layer cleans the apt lists the way the OCR layer does.
#
# PLAYWRIGHT_BROWSERS_PATH moves the download out of the installing user's home
# (root's, which uid 1000 cannot read) to one shared location, and stays set at
# runtime because that is where the launch looks. chmod gives the runtime user
# read and traverse on the root-owned tree.
#
# The layer sits after the dependency install, which provides the `playwright`
# command, and before the source copy, so editing the source never re-downloads
# the browser. This layer is 662 MB, second only to the dependency layer above.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /ms-playwright

# Application source. .dockerignore keeps data/, out/, models/, tests/golden/,
# .venv, .git and .env out of the image; tests/fixtures IS copied (the sample
# legislation a Corpus can be seeded from) and so is the built React bundle at
# src/regcompass/ui_dist, so the image never needs node.
COPY . .

# Install the project itself so the `regcompass` console script exists: the
# keyless demo (`regcompass run --engine fake ...`) and the reproduction path
# are command-line paths a judge runs inside the container. Editable, so there
# is exactly one copy of the source and one ui_dist in the image. --no-deps
# because the hash-pinned set above is already complete.
RUN pip install --no-deps -e .

# One writable place for everything a Run produces: the working database, the
# Corpus bytes under <economy>/raw/, and the exports. docker-compose mounts a
# named volume here. /app/data and /app/out are symlinks to it, so the
# command-line defaults (data/regcompass.db, out) land on the volume too and a
# `docker compose run` command sees the same database the server does.
RUN useradd --create-home --uid 1000 regcompass \
    && mkdir -p /data/out \
    && ln -s /data /app/data \
    && ln -s /data/out /app/out \
    && chown -R regcompass:regcompass /data

USER regcompass

EXPOSE 8000

# Liveness for compose and CI: the keyless status endpoint the interface itself
# polls. It reads the Engine and Economy registries and the in-process Run
# state only, so it answers before any Run and with no database present.
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=10 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/status', timeout=5)" || exit 1

# The one entry point: interface at / and API under /api. --host 0.0.0.0
# because the CLI default (127.0.0.1) is unreachable from the host. Runs reach
# Ollama at OLLAMA_HOST (set to http://ollama:11434 by docker-compose); a keyed
# Engine reads OPENROUTER_API_KEY from the environment or from a key typed into
# the Settings screen. No key is ever baked in.
CMD ["regcompass", "serve", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--db", "/data/regcompass.db", "--out", "/data/out", "--data-dir", "/data"]
