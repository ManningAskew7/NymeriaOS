import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
TAURI_CONFIG = ROOT / "nymeria-desktop" / "src-tauri" / "tauri.conf.json"
TAURI_PROCESS_MANAGER = ROOT / "nymeria-desktop" / "src-tauri" / "src" / "process_manager.rs"
FORBIDDEN_DESKTOP_BACKEND_BUNDLE_PATTERNS = (
    "pyinstaller",
    "nymeria-backend.spec",
    "nymeria-backend.exe",
    "verify_desktop_bundle_contract.py",
)


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


def _assert_no_desktop_backend_bundle_steps(commands: list[str]) -> None:
    command_text = "\n".join(commands).lower()
    for forbidden in FORBIDDEN_DESKTOP_BACKEND_BUNDLE_PATTERNS:
        assert forbidden.lower() not in command_text


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


def test_release_workflow_builds_windows_desktop_installer() -> None:
    jobs = _load_release_workflow()["jobs"]

    windows_job = jobs["windows-desktop"]
    assert windows_job["runs-on"] == "windows-latest"

    commands = _run_commands(windows_job)
    assert any(
        step.get("uses") == "actions/setup-node@v4" for step in windows_job["steps"]
    )
    assert any(
        "rustup toolchain install stable --profile minimal" in command
        for command in commands
    )
    assert "npm install" in commands
    assert "npm run tauri build" in commands
    _assert_no_desktop_backend_bundle_steps(commands)
    assert not any(
        "Nymeria/nymeria/frontend" in command
        or "nymeria-desktop/build" in command
        for command in commands
    )

    assert any(
        step.get("uses") == "actions/upload-artifact@v4"
        and step.get("with", {}).get("name") == "nymeria-windows-installer"
        and step.get("with", {}).get("path")
        == "nymeria-desktop/src-tauri/target/release/bundle/nsis/*.exe"
        for step in windows_job["steps"]
    )


def test_tauri_config_does_not_bundle_backend_resource() -> None:
    with TAURI_CONFIG.open("r", encoding="utf-8") as handle:
        config = json.load(handle)

    resources = json.dumps(config["bundle"].get("resources", {})).lower()
    assert "nymeria-backend.exe" not in resources
    assert "nymeria/dist" not in resources


def test_tauri_shell_does_not_discover_or_spawn_bundled_backend() -> None:
    source = TAURI_PROCESS_MANAGER.read_text(encoding="utf-8").lower()

    assert "nymeria-backend.exe" not in source
    assert "bundled_resource" not in source
    assert "has_bundled_backend" not in source
    assert "pyinstaller" not in source
    assert 'arg("run.py")' in source
    assert 'backend_command("api")' in source


def test_obsolete_desktop_bundle_contract_gate_is_removed() -> None:
    assert not (ROOT / "scripts" / "verify_desktop_bundle_contract.py").exists()
    assert not (ROOT / "Nymeria" / "tests" / "test_desktop_bundle_contract.py").exists()


def test_release_workflow_uploads_dist_files_to_github_release() -> None:
    jobs = _load_release_workflow()["jobs"]

    release_job = jobs["github-release"]
    commands = _run_commands(release_job)

    assert release_job["needs"] == ["python-package", "windows-desktop"]
    assert release_job["permissions"]["contents"] == "write"
    assert any(
        step.get("uses") == "actions/download-artifact@v4"
        and step.get("with", {}).get("name") == "nymeria-python-dist"
        for step in release_job["steps"]
    )
    assert any(
        step.get("uses") == "actions/download-artifact@v4"
        and step.get("with", {}).get("name") == "nymeria-windows-installer"
        for step in release_job["steps"]
    )
    assert any("gh release upload" in command for command in commands)
    assert any("gh release create" in command for command in commands)
    assert any("desktop-dist/*.exe" in command for command in commands)


def test_release_workflow_optionally_publishes_to_private_python_index() -> None:
    jobs = _load_release_workflow()["jobs"]

    private_index_job = jobs["private-python-index"]
    commands = _run_commands(private_index_job)
    publish_step = next(
        step
        for step in private_index_job["steps"]
        if step.get("name") == "Publish to private Python index"
    )

    assert publish_step["env"]["TWINE_REPOSITORY_URL"] == "${{ secrets.NYMERIA_PYPI_REPOSITORY_URL }}"
    assert publish_step["env"]["TWINE_USERNAME"] == "${{ secrets.NYMERIA_PYPI_USERNAME }}"
    assert publish_step["env"]["TWINE_PASSWORD"] == "${{ secrets.NYMERIA_PYPI_PASSWORD }}"
    assert any("Private Python index publish skipped" in command for command in commands)
    assert any("python -m twine upload --non-interactive --skip-existing" in command for command in commands)
