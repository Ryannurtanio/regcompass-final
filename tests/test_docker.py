"""The packaging files a judge's one command depends on.

`docker compose up` has to reach a working interface on a clean machine with
nothing but Docker: the image installs the `regcompass` console script, serves
the one app on 0.0.0.0:8000, answers a health check, carries no key and no
`.env`, and runs as a non-root user; compose pulls the Gate embedder by itself
and keeps the Corpus on a named volume. Those promises are made in text files,
so they are checked by reading the text files. The one test that actually
builds the image is opt-in (it needs a Docker daemon and several minutes).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
DOCKERIGNORE = (ROOT / ".dockerignore").read_text(encoding="utf-8")
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
COMPOSE_TEXT = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
CI = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
# YAML 1.1 reads a bare `on:` key as the boolean true, which is what a GitHub
# workflow's trigger block always is.
CI_TRIGGERS = CI.get("on", CI.get(True))
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")

APP_SERVICE = "regcompass"
DEMO_SERVICE = "regcompass-demo"
# The Economy the compose demo Runs, and the disclosure its rows carry, read off
# the code rather than repeated, so the compose file, the seed and the export can
# never drift apart.
from regcompass.export import FIXTURE_ADD_NOTE  # noqa: E402
from regcompass.fixtures import DEMO_ECONOMY  # noqa: E402


def _instructions(name: str) -> list[str]:
    """Every line of the Dockerfile that starts with the given instruction,
    continuation lines folded in."""
    folded = DOCKERFILE.replace("\\\n", " ")
    out = []
    for line in folded.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith(name.upper() + " "):
            out.append(" ".join(stripped.split()))
    return out


def _steps() -> list[tuple[str, str]]:
    """Every instruction in file order as (INSTRUCTION, rest), continuation
    lines folded in and comments dropped. Layer order is the question the plain
    `_instructions` view cannot answer."""
    folded = DOCKERFILE.replace("\\\n", " ")
    out: list[tuple[str, str]] = []
    for line in folded.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, _, rest = stripped.partition(" ")
        out.append((name.upper(), " ".join(rest.split())))
    return out


def _step_index(predicate) -> int:
    for i, (name, line) in enumerate(_steps()):
        if predicate(name, line):
            return i
    raise AssertionError("no instruction matched")


def _browsers_path() -> str | None:
    for line in _instructions("ENV"):
        match = re.search(r"PLAYWRIGHT_BROWSERS_PATH=(\S+)", line)
        if match:
            return match.group(1).strip("\"'")
    return None


def _ignored() -> set[str]:
    return {
        line.strip()
        for line in DOCKERIGNORE.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


# -- the image ------------------------------------------------------------


def test_image_starts_the_one_app_on_every_interface():
    """The CMD is `regcompass serve`, the one entry point the server work left, bound
    so the port publishes out of the container."""
    cmd = _instructions("CMD")
    assert len(cmd) == 1, "exactly one CMD, so what the container runs is unambiguous"
    # Exec form, so no shell sits between the container and the process.
    argv = json.loads(cmd[0][len("CMD ") :])
    assert argv[:2] == ["regcompass", "serve"]
    assert "0.0.0.0" in argv, "the CLI default is 127.0.0.1, unreachable from the host"
    assert "8000" in argv


def test_image_installs_the_console_script():
    """`docker compose run ... regcompass run --engine fake` is the keyless
    demo, so the console script has to exist inside the image."""
    installs = [line for line in _instructions("RUN") if "pip install" in line]
    assert any(
        re.search(r"pip install[^&]*\s(-e\s+\.|\.)(\s|$)", line) for line in installs
    ), "the project itself must be installed, not only its dependencies"


def test_image_has_a_health_check_on_the_status_endpoint():
    health = _instructions("HEALTHCHECK")
    assert len(health) == 1
    assert "/api/status" in health[0], "the keyless endpoint the interface itself polls"
    assert "--interval" in health[0] and "--start-period" in health[0]


def test_image_runs_as_a_non_root_user():
    users = _instructions("USER")
    assert users, "no USER line: the container would run as root"
    assert users[-1].split()[1] != "root"


def test_image_carries_no_key_and_no_env_file():
    assert ".env" in _ignored(), ".env must never enter the build context"
    assert not any(".env" in line for line in _instructions("COPY"))
    # A comment may name the variable; no instruction may give it a value.
    for instruction in ("ENV", "ARG", "RUN"):
        assert not any(
            "OPENROUTER_API_KEY" in line for line in _instructions(instruction)
        ), "no key is ever baked into the image"


def test_image_carries_the_ocr_engine_and_the_vendored_languages():
    """Tesseract is the OCR binary; the language data is vendored, so it must
    stay out of .dockerignore and the three files must be in the repository."""
    assert any("tesseract-ocr" in line for line in _instructions("RUN"))
    ignored = _ignored()
    assert "vendor/" not in ignored and "vendor" not in ignored
    names = {p.name for p in (ROOT / "vendor/tessdata").glob("*.traineddata")}
    assert {"eng.traineddata", "msa.traineddata", "lao.traineddata"} <= names


def test_image_carries_the_headless_browser_the_javascript_portals_need():
    """Discovery for a JavaScript Portal (Australia's, and the last rung of
    Singapore's escalation ladder) calls `chromium.launch(headless=True)` in
    src/regcompass/crawl.py. Without a browser in the image that is a host-only
    path. Chromium alone: firefox and webkit are never launched, so they are
    weight. The headless shell alone for the same reason: it is the binary a
    `headless=True` launch runs, and the full browser beside it is several
    hundred megabytes that nothing in this image would ever start."""
    installs = [line for line in _instructions("RUN") if "playwright install" in line]
    assert installs, "no browser install: Discovery for a JavaScript Portal would be host-only"
    assert len(installs) == 1, "one browser layer, so the download happens once"
    assert re.search(r"playwright install[^&|]*\bchromium\b", installs[0]), (
        "name the browser: a bare `playwright install` downloads all three"
    )
    for other in ("firefox", "webkit"):
        assert other not in installs[0], f"{other} is never launched by the code"
    assert "--only-shell" in installs[0], (
        "every launch in the code is headless, so the full browser is dead weight"
    )
    assert "--with-deps" in installs[0], "the browser needs its system libraries too"
    assert "rm -rf /var/lib/apt/lists" in installs[0], (
        "--with-deps installs through apt, so this layer cleans the lists like the OCR one"
    )


def test_the_browser_is_installed_where_the_non_root_user_can_read_it():
    """Installed as root, launched as uid 1000. Left at its default the download
    lands in the installing user's home, which the runtime user cannot read, so
    the path is moved out of any home and made readable. The variable stays set
    at runtime because that is where the launch looks for the browser."""
    path = _browsers_path()
    assert path, "PLAYWRIGHT_BROWSERS_PATH unset: the browser lands in root's home"
    assert not path.startswith("/root") and not path.startswith("~"), path
    assert path.startswith("/"), "an absolute path, independent of the working directory"
    installs = [line for line in _instructions("RUN") if "playwright install" in line]
    assert re.search(rf"chmod[^&|]*\ba\+rX\b[^&|]*{re.escape(path)}", installs[0]), (
        "the download is root-owned; uid 1000 needs read and traverse on it"
    )
    env_at = _step_index(lambda n, l: n == "ENV" and "PLAYWRIGHT_BROWSERS_PATH" in l)
    install_at = _step_index(lambda n, l: n == "RUN" and "playwright install" in l)
    assert env_at < install_at, "set the path before the download, or it goes somewhere else"


def test_the_browser_layer_sits_between_the_dependencies_and_the_source():
    """Layer caching: after the pip install (which provides the `playwright`
    command) and before `COPY . .`, so editing the source never re-downloads
    several hundred megabytes of browser."""
    deps_at = _step_index(lambda n, l: n == "RUN" and "requirements.txt" in l)
    install_at = _step_index(lambda n, l: n == "RUN" and "playwright install" in l)
    source_at = _step_index(lambda n, l: n == "COPY" and l.split()[0] == ".")
    assert deps_at < install_at < source_at


def test_image_ships_the_built_interface():
    """The React bundle is committed under src/, so it rides in with the source
    and the image never needs node."""
    assert (ROOT / "src/regcompass/ui_dist/index.html").is_file()
    ignored = _ignored()
    assert "ui/node_modules/" in ignored
    assert not any(part.startswith("src/regcompass/ui_dist") for part in ignored)


def _in_build_context(relpath: str) -> bool:
    """Docker's rule: every pattern is tested, the LAST one that matches wins,
    and a leading `!` re-includes. A trailing-slash pattern matches the
    directory and everything under it."""
    included = True
    for raw in DOCKERIGNORE.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        pattern = line.lstrip("!").strip().strip("/")
        if pattern.startswith("**/"):
            pattern = pattern[3:]
            hit = any(
                Path(relpath).match(pattern) or relpath.startswith(f"{parent}/{pattern}/")
                for parent in ("", *Path(relpath).parts[:-1])
            ) or f"/{pattern}/" in f"/{relpath}/"
        else:
            hit = relpath == pattern or relpath.startswith(pattern + "/")
        if hit:
            included = negated
    return included


def test_the_frozen_bundle_keeps_every_file_it_reads():
    """The judge-bundle profile serves audit_bundle/, whose manifest resolves
    each Document's PDF, canonical text, chunks, gate output and records into
    tests/. The bundle is read eagerly, so anything .dockerignore drops here is
    a FileNotFoundError in the container, not a missing extra."""
    manifest_path = ROOT / "audit_bundle/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    referenced: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and node.startswith("../"):
            referenced.add(node)

    walk(manifest)
    assert referenced, "the manifest should reference files outside the bundle"
    for ref in sorted(referenced):
        target = (manifest_path.parent / ref).resolve()
        rel = target.relative_to(ROOT).as_posix()
        assert target.exists(), f"{rel} is referenced by the bundle but missing"
        assert _in_build_context(rel), f".dockerignore drops {rel}, which the bundle reads"


def test_build_context_leaves_out_what_the_runtime_does_not_need():
    ignored = _ignored()
    for entry in ("data/", "out/", "models/", ".venv/", ".git/", "tests/golden/"):
        assert entry in ignored, f"{entry} should not enter the build context"


# -- compose --------------------------------------------------------------


def test_compose_runs_the_app_the_ollama_service_and_the_embedder_pull():
    assert set(COMPOSE["services"]) >= {APP_SERVICE, "ollama", "ollama-init"}


def test_compose_pulls_the_embedder_by_itself():
    """Acceptance box 2: no manual `ollama pull` step anywhere."""
    init = COMPOSE["services"]["ollama-init"]
    command = " ".join(init["command"]) if isinstance(init["command"], list) else init["command"]
    assert "pull" in command and "bge-m3" in command
    assert init.get("restart", "no") == "no", "a one-shot init must not restart forever"
    app = COMPOSE["services"][APP_SERVICE]
    assert app["depends_on"]["ollama-init"]["condition"] == "service_completed_successfully"


def test_compose_health_checks_the_app():
    health = COMPOSE["services"][APP_SERVICE].get("healthcheck")
    assert health is None or "test" in health, "inherit the image check or declare one"
    dockerfile_health = _instructions("HEALTHCHECK")
    assert dockerfile_health, "the image carries the check compose reports on"


def test_compose_publishes_the_known_port():
    assert "8000:8000" in COMPOSE["services"][APP_SERVICE]["ports"]


def test_compose_keeps_the_corpus_and_the_models_on_named_volumes():
    """Named volumes, not bind mounts: a clean machine has no ./data to mount
    and the pre-run database ships inside the image."""
    assert set(COMPOSE["volumes"]) >= {"regcompass-data", "ollama-models"}
    mounts = COMPOSE["services"][APP_SERVICE]["volumes"]
    assert any(str(m).startswith("regcompass-data:") for m in mounts)
    assert not any(str(m).startswith("./") for m in mounts), "no bind mount on the app"


def _env_map(service: str) -> dict[str, str | None]:
    """A service's environment as a mapping, whichever of Compose's two forms
    the file uses. A bare list entry (`- VAR`) is a pass-through and maps to
    None: Compose fills it from the machine, and nothing in the file supplies a
    value for it."""
    env = COMPOSE["services"][service].get("environment", {})
    if isinstance(env, dict):
        return dict(env)
    out: dict[str, str | None] = {}
    for entry in env:
        name, sep, value = str(entry).partition("=")
        out[name] = value if sep else None
    return out


def test_compose_passes_the_key_through_without_storing_it():
    env = _env_map(APP_SERVICE)
    assert "OPENROUTER_API_KEY" in env
    assert env["OPENROUTER_API_KEY"] is None, (
        "pass-through only: a `${OPENROUTER_API_KEY:-}` default is interpolated"
        " from the project's own .env, which is how a demo from a checkout"
        " silently went live (defect 1)"
    )
    assert "sk-" not in COMPOSE_TEXT, "no key literal in a committed file"


def test_no_service_defaults_the_key_from_the_project_env_file():
    """Compose reads ./.env for interpolation, so any `${OPENROUTER_API_KEY...}`
    in this file hands a developer's key to a container that asked for none."""
    assert "${OPENROUTER_API_KEY" not in COMPOSE_TEXT
    for name, service in COMPOSE["services"].items():
        assert "env_file" not in service, (
            f"{name}: env_file does not stop project .env interpolation (measured)"
            " and would put a key file into the container"
        )


def test_the_keyless_demo_service_sets_an_explicit_empty_key():
    """The one service a judge is told to run must be keyless by construction,
    not keyless by the absence of a .env that a checkout in fact has."""
    demo = COMPOSE["services"][DEMO_SERVICE]
    env = _env_map(DEMO_SERVICE)
    assert env["OPENROUTER_API_KEY"] == "", "an explicit empty string, not a pass-through"
    assert "demo" in demo["profiles"], "off by default"
    assert "depends_on" not in demo, "the fake Engine needs neither Ollama nor the embedder"


def test_the_keyless_demo_seeds_the_corpus_before_it_runs():
    """Defect 2: on a clean volume the demo exited 2 with an empty Corpus and
    sent the judge to a network Discovery. Seed, Run, Export, in that order."""
    demo = COMPOSE["services"][DEMO_SERVICE]
    command = " ".join(demo["command"]) if isinstance(demo["command"], list) else demo["command"]
    steps = [command.index(s) for s in ("regcompass seed", "regcompass run", "regcompass export")]
    assert all(i >= 0 for i in steps) and steps == sorted(steps), command
    assert f"--economy {DEMO_ECONOMY}" in command
    assert "--engine fake" in command


def test_the_demo_writes_to_the_same_volume_the_app_serves_from():
    """So the Run a judge just made is in the interface when they open it."""
    mounts = COMPOSE["services"][DEMO_SERVICE]["volumes"]
    assert any(str(m).startswith("regcompass-data:") for m in mounts)


def test_the_bundled_legislation_the_demo_seeds_is_in_the_build_context():
    """`regcompass seed` reads these files off disk inside the container, so
    .dockerignore dropping one is a broken judge path, not a missing extra."""
    from regcompass.fixtures import BUNDLED

    seeded = [doc for docs in BUNDLED.values() for doc in docs]
    assert seeded, "no bundled legislation to seed"
    for doc in seeded:
        rel = doc.path.relative_to(ROOT).as_posix()
        assert doc.path.is_file(), f"{rel} is missing from the repository"
        assert _in_build_context(rel), f".dockerignore drops {rel}, which `seed` reads"


def test_compose_offers_the_keyless_bundle_view_behind_a_profile():
    """The Round 1 evidence view needs neither a key nor a Run, and must not
    start by default."""
    bundle = COMPOSE["services"]["regcompass-bundle"]
    assert "judge-bundle" in bundle["profiles"]
    command = " ".join(bundle["command"]) if isinstance(bundle["command"], list) else bundle["command"]
    assert "--bundle" in command


def test_compose_points_the_app_at_the_ollama_service():
    assert _env_map(APP_SERVICE)["OLLAMA_HOST"] == "http://ollama:11434"


def test_compose_file_names_no_manual_pull_step():
    """The instruction a judge reads at the top of the file must match what the
    file does."""
    assert "compose exec ollama ollama pull" not in COMPOSE_TEXT


def test_readme_names_no_manual_pull_step():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docker compose exec ollama ollama pull" not in readme


# -- CI -------------------------------------------------------------------


def test_ci_runs_on_the_final_round_branch():
    assert "final-round" in CI_TRIGGERS["push"]["branches"]


def test_ci_builds_the_image_and_smoke_tests_it():
    jobs = CI["jobs"]
    assert "docker" in jobs, "a broken Dockerfile must fail CI"
    steps = " ".join(str(step.get("run", "")) for step in jobs["docker"]["steps"])
    assert "docker build" in steps
    assert "docker compose config" in steps, "the compose file is validated too"
    assert "/api/status" in steps, "the health endpoint is the smoke test"
    assert "push" not in steps, "CI builds the image, it does not publish it"


def test_make_has_the_docker_entry_points():
    assert "docker-build:" in MAKEFILE
    assert "docker-up:" in MAKEFILE
    assert "docker-demo:" in MAKEFILE, "the keyless path needs an entry point too"


# -- the real build (opt in) ----------------------------------------------


def _docker_ready() -> bool:
    # Checked only when the build is opted into, so collecting the suite never
    # shells out to a daemon that is probably not running.
    if os.environ.get("REGCOMPASS_DOCKER_BUILD") != "1":
        return False
    if shutil.which("docker") is None:
        return False
    return subprocess.run(
        ["docker", "info"], capture_output=True, timeout=60
    ).returncode == 0


needs_docker_build = pytest.mark.skipif(
    os.environ.get("REGCOMPASS_DOCKER_BUILD") != "1",
    reason="opt in with REGCOMPASS_DOCKER_BUILD=1 (a full image build takes minutes)",
)
needs_docker_daemon = pytest.mark.skipif(
    not _docker_ready(), reason="no reachable Docker daemon"
)

# One tag for everything this file builds, so a parallel worktree's images and
# this one never collide and the cleanup knows exactly what it owns.
TAG = "regcompass:dockersuite"


@pytest.mark.slow
@needs_docker_build
@needs_docker_daemon
def test_the_image_builds_and_the_console_script_answers():
    tag = "regcompass:pytest"
    build = subprocess.run(
        ["docker", "build", "-t", tag, "."], cwd=ROOT, capture_output=True, text=True, timeout=3600
    )
    assert build.returncode == 0, build.stderr[-4000:]
    for argv in (
        ["regcompass", "--help"],
        ["tesseract", "--version"],
        ["python", "-c", "import pathlib; assert pathlib.Path('/app/vendor/tessdata/lao.traineddata').is_file()"],
        ["sh", "-c", "test ! -f /app/.env"],
    ):
        run = subprocess.run(
            ["docker", "run", "--rm", tag, *argv], capture_output=True, text=True, timeout=300
        )
        assert run.returncode == 0, f"{argv}: {run.stderr[-2000:]}"


# The smallest thing that proves the browser is really there and really usable
# by the runtime user: start it, open the empty page, read the title back, close
# it. No page is fetched, so the container needs no network at all.
_LAUNCH_CHROMIUM = """
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    try:
        page = browser.new_page()
        page.goto("about:blank")
        print("TITLE=[%s]" % page.title())
    finally:
        browser.close()
print("BROWSER_OK")
"""


@pytest.mark.slow
@needs_docker_build
@needs_docker_daemon
def test_the_browser_launches_inside_the_container_as_the_default_user(built_image):
    """Reading the Dockerfile shows the browser was downloaded; only running it
    shows uid 1000 can launch it. `--network none` proves the launch itself
    reaches nothing, so Discovery in the container fails for Portal reasons or
    not at all, never for a missing browser."""
    run = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", TAG, "python", "-c", _LAUNCH_CHROMIUM],
        capture_output=True, text=True, timeout=600,
    )
    assert run.returncode == 0, run.stderr[-3000:]
    assert "TITLE=[" in run.stdout, run.stdout[-1000:]
    assert "BROWSER_OK" in run.stdout, run.stdout[-1000:]


# -- the real build: the two defects the first verification found -----------
#
# Both are about what a container actually receives, which no amount of reading
# the compose file can settle: defect 1 was a key arriving from the project's
# own .env, defect 2 was an empty Corpus on a clean volume. So these tests build
# the image, start containers from THIS checkout (the .env included, if there is
# one) and measure. A key's VALUE is never read, printed or compared: only the
# length of the variable inside the container, which is what the question is.


def _dotenv_declares_the_key() -> bool:
    """Whether this checkout's .env declares the key at all, the precondition
    that makes the length-0 assertions proof rather than a tautology. The file's
    CONTENTS are never read: grep counts matching lines and nothing else."""
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return False
    return subprocess.run(
        ["grep", "-c", "^OPENROUTER_API_KEY=", str(env_path)],
        capture_output=True, text=True, timeout=30,
    ).stdout.strip() not in ("", "0")


@pytest.fixture(scope="module")
def built_image(tmp_path_factory):
    """The image under test, plus a compose override that pins every service to
    it, so `docker compose run` starts THIS build and never rebuilds."""
    build = subprocess.run(
        ["docker", "build", "-t", TAG, "."],
        cwd=ROOT, capture_output=True, text=True, timeout=3600,
    )
    assert build.returncode == 0, build.stderr[-4000:]
    override = tmp_path_factory.mktemp("compose") / "image-override.yml"
    override.write_text(
        yaml.safe_dump(
            {
                "services": {
                    name: {"image": TAG}
                    for name in (APP_SERVICE, DEMO_SERVICE, "regcompass-bundle")
                }
            }
        ),
        encoding="utf-8",
    )
    return override


def _compose(override: Path, project: str, *args, env: dict | None = None, timeout: int = 900):
    """One compose command against this checkout's file. The project name is
    ours alone, so the named volume Compose creates is ours to remove."""
    argv = ["docker", "compose", "-p", project, "-f", str(ROOT / "docker-compose.yml"), "-f", str(override), *args]
    return subprocess.run(
        argv, cwd=ROOT, capture_output=True, text=True, timeout=timeout,
        env={**os.environ, **(env or {})},
    )


def _down(override: Path, project: str) -> None:
    _compose(override, project, "down", "-v", "--remove-orphans", timeout=300)


def _key_length_in(override: Path, project: str, service: str, *, env=None, extra=()) -> int:
    """The length of OPENROUTER_API_KEY inside a container of this service. The
    value never leaves the container: the shell prints its length."""
    run = _compose(
        override, project, *extra, "run", "--rm", "--no-deps", service,
        "sh", "-c", "echo len=${#OPENROUTER_API_KEY}", env=env, timeout=600,
    )
    assert run.returncode == 0, run.stderr[-2000:]
    match = re.search(r"len=(\d+)", run.stdout)
    assert match, f"no length line in the container output: {run.stdout[-500:]}"
    return int(match.group(1))


@pytest.mark.slow
@needs_docker_build
@needs_docker_daemon
class TestDefect1TheDemoIsKeylessFromThisCheckout:
    def test_the_demo_container_receives_no_key(self, built_image):
        """The defect exactly: run from a checkout whose .env holds a key, the
        container used to get it. An explicit empty string in the demo service
        is what makes this 0 rather than however long that key is."""
        project = "dockersuitekey"
        try:
            length = _key_length_in(built_image, project, DEMO_SERVICE)
        finally:
            _down(built_image, project)
        assert length == 0, (
            "the keyless demo received a key"
            f" (this checkout's .env declares one: {_dotenv_declares_the_key()})"
        )

    def test_the_app_container_receives_a_key_exported_in_the_shell(self, built_image):
        """Pass-through still works, which is the other half of the contract: a
        judge who HAS a key exports it and compose hands it over. The value here
        is a dummy this test invents; the real one is never read."""
        dummy = "x" * 12
        project = "dockersuiteshell"
        try:
            length = _key_length_in(
                built_image, project, APP_SERVICE, env={"OPENROUTER_API_KEY": dummy}
            )
        finally:
            _down(built_image, project)
        assert length == len(dummy)

    def test_an_empty_env_file_reproduces_a_clean_machine(self, built_image):
        """The recipe the compose header and the README give for running from a
        checkout as a judge would. `env_file: []` on the service does NOT do
        this (measured); --env-file does."""
        project = "dockersuiteclean"
        try:
            length = _key_length_in(
                built_image, project, APP_SERVICE, extra=("--env-file", "/dev/null")
            )
        finally:
            _down(built_image, project)
        assert length == 0, (
            "--env-file /dev/null still let a key through"
            f" (this checkout's .env declares one: {_dotenv_declares_the_key()})"
        )


@pytest.mark.slow
@needs_docker_build
@needs_docker_daemon
class TestDefect2TheDemoRunsOnACleanVolume:
    @pytest.mark.parametrize("economy", [DEMO_ECONOMY, "SG"])
    def test_the_documented_demo_command_seeds_runs_and_exports(self, built_image, economy):
        """The whole judge path on a volume that has never existed before: seed
        the Corpus from the bundled legislation, Run the fake Engine, Export.
        Before the seed step this exited 2 on an empty Corpus.

        Both README blocks are covered: the service's own default command, and
        the one a judge types to pick another Economy. Singapore is the one
        whose Run produces duplicate provision rows, so it only exports at all
        because of the duplicate collapse."""
        project = f"dockersuitedemo{economy.lower()}"
        _down(built_image, project)  # a fresh volume, whatever a previous run left
        override_command = (
            []
            if economy == DEMO_ECONOMY
            else [
                "sh", "-c",
                f"regcompass seed --economy {economy}"
                f" && regcompass run --engine fake --economy {economy} --pillar 7"
                " && regcompass export",
            ]
        )
        try:
            demo = _compose(
                built_image, project, "run", "--rm", "--no-deps", DEMO_SERVICE,
                *override_command, timeout=2400,
            )
            assert demo.returncode == 0, (demo.stdout[-3000:], demo.stderr[-3000:])
            assert "battery GREEN" in demo.stdout, demo.stdout[-2000:]

            listed = _compose(
                built_image, project, "run", "--rm", "--no-deps", DEMO_SERVICE,
                "sh", "-c",
                "test -s /data/out/submission.csv && test -s /data/out/submission.json"
                " && test -s /data/regcompass.db && echo EXPORTS_PRESENT",
                timeout=600,
            )
            assert listed.returncode == 0, listed.stderr[-2000:]
            assert "EXPORTS_PRESENT" in listed.stdout

            # The written file says the Corpus was seeded. A reader of the
            # submission has no other way to learn it.
            disclosed = _compose(
                built_image, project, "run", "--rm", "--no-deps", DEMO_SERVICE,
                "sh", "-c",
                f"grep -qF '{FIXTURE_ADD_NOTE}' /data/out/submission.csv && echo DISCLOSED",
                timeout=600,
            )
            assert disclosed.returncode == 0, disclosed.stderr[-2000:]
            assert "DISCLOSED" in disclosed.stdout
        finally:
            _down(built_image, project)

    def test_seeding_twice_on_the_same_volume_still_succeeds(self, built_image):
        """A judge's second `docker compose run` must not fail on the Documents
        the first one added."""
        project = "dockersuitetwice"
        _down(built_image, project)
        try:
            for _ in range(2):
                seeded = _compose(
                    built_image, project, "run", "--rm", "--no-deps", DEMO_SERVICE,
                    "regcompass", "seed", "--economy", DEMO_ECONOMY, timeout=1200,
                )
                assert seeded.returncode == 0, seeded.stderr[-2000:]
            assert "already seeded" in seeded.stdout, seeded.stdout[-1000:]
        finally:
            _down(built_image, project)
