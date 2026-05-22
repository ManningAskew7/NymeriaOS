from __future__ import annotations

from pathlib import Path

from nymeria.tools.bash import bash_execute
from nymeria.tools.execution_environment import (
    ExecutionEnvironment,
    configure_environment_aware_tool_descriptions,
)
from nymeria.tools.filesystem import file_read, file_write
from nymeria.tools.file_edit import file_edit


class _Settings:
    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.nymeria_confine_file_to_workspace = False


def test_bash_execute_defaults_to_project_root(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        "nymeria.tools.execution_environment.get_settings",
        lambda: _Settings(project),
    )

    result = bash_execute.func("pwd")

    assert result.strip() == str(project)


def test_bash_execute_resolves_relative_working_directory_from_project_root(
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "project"
    nested = project / "src" / "nymeria"
    nested.mkdir(parents=True)
    monkeypatch.setattr(
        "nymeria.tools.execution_environment.get_settings",
        lambda: _Settings(project),
    )

    result = bash_execute.func("pwd", working_directory="src/nymeria")

    assert result.strip() == str(nested)


def test_file_tools_resolve_relative_paths_from_project_root(tmp_path, monkeypatch):
    project = tmp_path / "project"
    target = project / "notes" / "todo.txt"
    target.parent.mkdir(parents=True)
    target.write_text("alpha beta\n", encoding="utf-8")
    monkeypatch.setattr(
        "nymeria.tools.execution_environment.get_settings",
        lambda: _Settings(project),
    )

    assert file_read.func("notes/todo.txt").strip() == "alpha beta"

    write_result = file_write.func("notes/generated.txt", "created")
    assert write_result.startswith("[Success]")
    assert (project / "notes" / "generated.txt").read_text(encoding="utf-8") == "created"

    edit_result = file_edit.func(
        "notes/todo.txt",
        [{"operation": "replace", "old_text": "beta", "new_text": "BETA"}],
    )
    assert '"ok": true' in edit_result
    assert target.read_text(encoding="utf-8") == "alpha BETA\n"


def test_dynamic_descriptions_include_execution_context():
    env = ExecutionEnvironment(
        platform_label="TestOS 1",
        default_cwd="/repo/root",
        process_cwd="/service",
        shell_executable="/bin/sh",
        available_shells=("bash", "sh"),
        in_container=True,
        path_separator="/",
    )

    configure_environment_aware_tool_descriptions(
        [bash_execute, file_read, file_write, file_edit],
        env,
    )

    assert "Default cwd when working_directory is omitted: /repo/root" in (
        bash_execute.description or ""
    )
    assert "Available shells: bash, sh" in (bash_execute.description or "")
    assert "Relative file_path values resolve from: /repo/root" in (
        file_read.description or ""
    )
    assert "Shell cd commands do not change file tool paths" in (
        file_edit.description or ""
    )
