# Developer entry points. Judges never need make: their path is the README's
# pip instructions (Tier A). These targets are the module-loop gates.

.PHONY: test licenses secrets lock check docker-build docker-up docker-demo docker-load-data

test:
	uv run pytest -q

# License floor: Apache-2.0-compatible only in the shipped dependency set.
# pip-licenses scans the synced environment (base + live + dev).
# sentencepiece is exempted from the metadata scan because its wheel carries no
# license metadata (reads UNKNOWN); the project license is Apache-2.0, verified
# 5 Jul 2026 at https://github.com/google/sentencepiece/blob/master/LICENSE.
licenses:
	uv run pip-licenses --partial-match --ignore-packages sentencepiece --allow-only="MIT;MIT License;BSD;BSD License;BSD-3-Clause;BSD-2-Clause;Apache;Apache-2.0;Apache Software License;Apache License 2.0;ISC;ISC License;Python Software Foundation License;PSF-2.0;Mozilla Public License 2.0;MPL-2.0;The Unlicense;Zero-Clause BSD;CMU License (MIT-CMU);Historical Permission Notice and Disclaimer"

secrets:
	gitleaks git --redact --config .gitleaks.toml

lock:
	uv lock
	uv export --format requirements-txt --all-extras --no-dev --no-emit-project -o requirements.txt

check: test licenses secrets

# The judge's path, from this repository. docker-up is the one command:
# it builds, pulls the Gate embedder and serves http://localhost:8000 .
docker-build:
	docker build -t regcompass:local .

docker-up:
	docker compose up --build

# The keyless path, end to end: seed the Corpus offline from the bundled
# legislation, Run the fake Engine, Export. No key, no network, no Ollama.
docker-demo:
	docker compose run --rm --no-deps regcompass-demo

# The pre-run Runs, their Corpus and source files: the release's prepared-data
# archive, SHA-256 checked, into the app's volume. Before the first docker-up.
docker-load-data:
	docker compose run --rm --no-deps regcompass-load
