from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _load_ci_workflow() -> dict:
    with CI_WORKFLOW.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _run_commands(job: dict) -> list[str]:
    commands = []
    for step in job["steps"]:
        run = step.get("run")
        if run:
            commands.append(" ".join(run.split()))
    return commands


def test_ci_validates_docker_compose_and_dockerfile() -> None:
    jobs = _load_ci_workflow()["jobs"]

    docker_job = jobs["docker-validation"]
    assert docker_job["env"]["DISCORD_BOT_TOKEN"] == "disabled"
    assert docker_job["env"]["TELEGRAM_BOT_TOKEN"] == "disabled"
    assert docker_job["env"]["TWITCH_CHANNEL"] == "channelname"
    assert any(
        step.get("uses") == "docker/setup-buildx-action@v3"
        for step in docker_job["steps"]
    )

    commands = _run_commands(docker_job)
    assert (
        "docker compose --env-file .env.docker.example -f docker-compose.yml "
        "config --quiet"
    ) in commands
    assert (
        "docker compose --profile discord --profile telegram --profile twitch "
        "--profile voice --env-file .env.docker.example -f docker-compose.yml "
        "config --quiet"
    ) in commands
    assert "docker buildx build --check -f Dockerfile.full ." in commands
    assert "docker buildx build --check -f Dockerfile.slim ." in commands


def test_ci_installs_dev_requirements_for_backend_tests() -> None:
    jobs = _load_ci_workflow()["jobs"]

    backend_job = jobs["backend-tests"]
    commands = _run_commands(backend_job)

    assert any("Nymeria/requirements-dev.txt" in command for command in commands)


def test_ci_runs_backend_lint_and_coverage_gates() -> None:
    jobs = _load_ci_workflow()["jobs"]

    lint_commands = _run_commands(jobs["lint"])
    assert (
        "python -m ruff check Nymeria/nymeria Nymeria/tests Nymeria/run.py"
        in lint_commands
    )

    backend_commands = _run_commands(jobs["backend-tests"])
    assert (
        "python -m pytest Nymeria/tests --cov=nymeria --cov=run "
        "--cov-report=term --cov-report=xml:Nymeria/coverage.xml "
        "--cov-fail-under=38"
    ) in backend_commands
