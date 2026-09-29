"""What a release tag publishes, and how the prepared database reaches the
container. Both are promises made in text files, so they are checked by
reading the text files, the way tests/test_docker.py checks the image."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
CI_TRIGGERS = CI.get("on", CI.get(True))
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")
LOAD_SERVICE = "regcompass-load"


def _run_text(job: dict) -> str:
    return "\n".join(str(step.get("run", "")) for step in job["steps"])


def _uses(job: dict) -> list[str]:
    return [str(step.get("uses", "")) for step in job["steps"]]


# -- the tag --------------------------------------------------------------


def test_a_release_tag_triggers_the_workflow():
    tags = CI_TRIGGERS["push"]["tags"]
    assert "final-round" in tags
    assert "v*" in tags


def test_branch_pushes_still_trigger_the_same_branches():
    assert CI_TRIGGERS["push"]["branches"] == ["main", "dev", "m3-translation", "final-round"]
    assert "pull_request" in CI_TRIGGERS


def test_the_release_job_runs_on_tags_only_and_after_every_check():
    job = CI["jobs"]["release"]
    assert job["if"] == "startsWith(github.ref, 'refs/tags/')"
    assert set(job["needs"]) == {"gitleaks", "test", "docker"}


def test_the_release_job_alone_may_write_packages_and_releases():
    assert CI["jobs"]["release"]["permissions"] == {"contents": "write", "packages": "write"}
    assert "permissions" not in CI, "no workflow-wide grant"
    for name, job in CI["jobs"].items():
        if name != "release":
            assert "permissions" not in job, f"{name} publishes nothing"


def test_the_image_is_pushed_to_ghcr_under_the_tag_with_the_workflow_token():
    job = CI["jobs"]["release"]
    login = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("docker/login-action"))
    assert login["with"]["registry"] == "ghcr.io"
    assert login["with"]["password"] == "${{ secrets.GITHUB_TOKEN }}", "no personal token"
    push = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("docker/build-push-action"))
    assert push["with"]["push"] is True
    assert push["with"]["tags"].endswith(":${{ github.ref_name }}")
    assert "org.opencontainers.image.source=" in push["with"]["labels"]
    assert "linux/amd64" in push["with"]["platforms"]


def test_the_image_name_is_lowercased_for_the_registry():
    assert "${GITHUB_REPOSITORY,,}" in _run_text(CI["jobs"]["release"])


def test_a_release_is_opened_on_the_tag_with_the_commit_and_digest():
    text = _run_text(CI["jobs"]["release"])
    assert "gh release create" in text and "--verify-tag" in text
    assert "gh release view" in text, "an existing Release is left alone"
    assert "GITHUB_SHA" in text and "DIGEST" in text


def test_the_release_job_never_builds_or_uploads_the_prepared_data():
    text = _run_text(CI["jobs"]["release"])
    assert "pack_prepared_data" not in text
    assert "gh release upload" not in text


def test_the_branch_smoke_test_job_is_unchanged_and_publishes_nothing():
    job = CI["jobs"]["docker"]
    assert "permissions" not in job
    assert not any(u.startswith(("docker/login-action", "docker/build-push-action"))
                   for u in _uses(job))


# -- the prepared database in compose ------------------------------------


def test_compose_offers_the_load_behind_a_profile():
    service = COMPOSE["services"][LOAD_SERVICE]
    assert "prepared" in service["profiles"], "off by default"
    assert service["command"] == ["regcompass", "load-data"]
    assert service.get("restart") == "no"
    assert "depends_on" not in service, "a download needs neither Ollama nor the app"


def test_the_load_fills_the_volume_the_app_serves_from():
    service = COMPOSE["services"][LOAD_SERVICE]
    app = COMPOSE["services"]["regcompass"]
    assert "regcompass-data:/data" in service["volumes"]
    app_env = {e.split("=", 1)[0]: e.split("=", 1)[1] for e in app["environment"] if "=" in e}
    assert service["environment"]["REGCOMPASS_DB"] == app_env["REGCOMPASS_DB"]
    assert service["environment"]["REGCOMPASS_DATA"] == app_env["REGCOMPASS_DATA"]


def test_the_load_is_keyless():
    assert COMPOSE["services"][LOAD_SERVICE]["environment"]["OPENROUTER_API_KEY"] == ""


def test_make_has_the_load_entry_point():
    assert "docker-load-data:" in MAKEFILE
    assert "regcompass-load" in MAKEFILE


def test_the_release_data_document_names_the_commands_it_promises():
    text = (ROOT / "docs" / "RELEASE_DATA.md").read_text(encoding="utf-8")
    assert "docker compose run --rm --no-deps regcompass-load" in text
    assert "regcompass load-data" in text
    assert "scripts/pack_prepared_data.py" in text
    assert "gh release upload" in text
