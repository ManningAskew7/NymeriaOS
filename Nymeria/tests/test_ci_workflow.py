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
