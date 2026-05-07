from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def _load_release_workflow() -> dict:
    with RELEASE_WORKFLOW.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _run_commands(job: dict) -> list[str]:
    commands = []
    for step in job["steps"]:
        run = step.get("run")
        if run:
            commands.append(" ".join(run.split()))
    return commands


def test_release_workflow_runs_on_version_tags() -> None:
    workflow = _load_release_workflow()

    triggers = workflow["on"]
    assert triggers["push"]["tags"] == ["v*"]
    assert "workflow_dispatch" in triggers


def test_release_workflow_builds_frontend_into_python_package() -> None:
    jobs = _load_release_workflow()["jobs"]

    build_job = jobs["python-package"]
    commands = _run_commands(build_job)

    assert any(step.get("uses") == "actions/setup-node@v4" for step in build_job["steps"])
    assert any(command == "npm install" for command in commands)
    assert any(command == "npm run build" for command in commands)
    assert any(
        "cp -a nymeria-desktop/build/. Nymeria/nymeria/frontend/" in command
        for command in commands
    )
    assert any("test -f Nymeria/nymeria/frontend/index.html" in command for command in commands)
    assert any("test -d Nymeria/nymeria/frontend/_app" in command for command in commands)


def test_release_workflow_builds_and_checks_python_distributions() -> None:
    jobs = _load_release_workflow()["jobs"]

    build_job = jobs["python-package"]
    commands = _run_commands(build_job)

    assert 'python scripts/sync_versions.py --check --tag "$GITHUB_REF_NAME"' in commands
    assert "python -m build" in commands
    assert "python -m twine check dist/*.whl dist/*.tar.gz" in commands
    assert any(
        step.get("uses") == "actions/upload-artifact@v4"
        and step.get("with", {}).get("name") == "nymeria-python-dist"
        and "Nymeria/dist/*.whl" in step.get("with", {}).get("path", "")
        and "Nymeria/dist/*.tar.gz" in step.get("with", {}).get("path", "")
        for step in build_job["steps"]
    )


def test_release_workflow_uploads_dist_files_to_github_release() -> None:
    jobs = _load_release_workflow()["jobs"]

    release_job = jobs["github-release"]
    commands = _run_commands(release_job)

    assert release_job["permissions"]["contents"] == "write"
    assert any(
        step.get("uses") == "actions/download-artifact@v4"
        and step.get("with", {}).get("name") == "nymeria-python-dist"
        for step in release_job["steps"]
    )
    assert any("gh release upload" in command for command in commands)
    assert any("gh release create" in command for command in commands)


def test_release_workflow_optionally_publishes_to_private_python_index() -> None:
    jobs = _load_release_workflow()["jobs"]

    private_index_job = jobs["private-python-index"]
    commands = _run_commands(private_index_job)
    publish_step = next(
        step for step in private_index_job["steps"] if step.get("name") == "Publish to private Python index"
    )

    assert publish_step["env"]["TWINE_REPOSITORY_URL"] == "${{ secrets.NYMERIA_PYPI_REPOSITORY_URL }}"
    assert publish_step["env"]["TWINE_USERNAME"] == "${{ secrets.NYMERIA_PYPI_USERNAME }}"
    assert publish_step["env"]["TWINE_PASSWORD"] == "${{ secrets.NYMERIA_PYPI_PASSWORD }}"
    assert any("Private Python index publish skipped" in command for command in commands)
    assert any("python -m twine upload --non-interactive --skip-existing" in command for command in commands)
