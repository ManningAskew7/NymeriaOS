"""The runtime settings file (``NYMERIA_SETTINGS_FILE``, #254).

On the container shapes the project root is the image tree ``/app``, so a
settings change made in the app used to land in ``/app/.env``: the container's
writable layer, discarded by every ``up -d`` recreate. The shapes now name a
file on the data volume, which is where writes go and which loads last.

The loader writes ``os.environ`` directly; conftest hermeticity layer 3
restores the environment per test (see test_settings_env_loading.py).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nymeria.config import settings as settings_mod


@pytest.fixture(autouse=True)
def _isolated_env_loading():
    settings_mod.reset_env_loading_state_for_tests()
    yield
    settings_mod.reset_env_loading_state_for_tests()


@pytest.fixture
def container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A container-shaped layout: project root /app, data volume /data.

    The loader only appends the runtime file for THIS process's root, so the
    process root is pointed at the fake /app. The suite's load suppression
    ("no un-suppress", #294) is lifted here, the one sanctioned exception: it
    guards the REAL checkout's files, and both roots are patched to tmp, so
    nothing real can load.
    """
    app = tmp_path / "app"
    data = tmp_path / "data"
    app.mkdir()
    data.mkdir()
    monkeypatch.setattr(settings_mod, "PROJECT_ROOT", app)
    monkeypatch.setattr(settings_mod, "_PROCESS_ROOT", app)
    monkeypatch.setattr(settings_mod, "_env_file_loading_suppressed", False)
    runtime = data / "settings.env"
    monkeypatch.setenv("NYMERIA_SETTINGS_FILE", str(runtime))
    return app, runtime


def test_settings_writes_go_to_the_runtime_file_not_the_image_tree(container):
    app, runtime = container
    # Even with a root dotenv present (the old write target), the shape's
    # durable file wins.
    (app / ".env").write_text("LLM_MODEL=x\n", encoding="utf-8")

    assert settings_mod.get_env_write_path(app) == runtime
    assert settings_mod.get_env_write_path() == runtime


def test_unset_leaves_the_write_path_and_load_list_as_they_were(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings_mod, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(settings_mod, "_PROCESS_ROOT", tmp_path)
    monkeypatch.delenv("NYMERIA_SETTINGS_FILE", raising=False)
    (tmp_path / "config.env").write_text("LLM_MODEL=x\n", encoding="utf-8")

    assert settings_mod.runtime_settings_file() is None
    assert settings_mod.get_env_write_path(tmp_path) == tmp_path / "config.env"
    assert settings_mod.get_env_file_paths(tmp_path) == tuple(
        tmp_path / name for name in settings_mod.ENV_FILENAMES
    )


def test_it_loads_last_and_wins_over_root_files_and_the_container_env(container):
    app, runtime = container
    (app / ".env").write_text(
        "LLM_MODEL=from-root-dotenv\nLLM_TEMPERATURE=0.7\n", encoding="utf-8"
    )
    os.environ["LLM_REASONING_EFFORT"] = "low"  # compose-injected, say
    runtime.write_text(
        "LLM_MODEL=saved-in-app\nLLM_REASONING_EFFORT=high\nTWITCH_CHANNEL=newkey\n",
        encoding="utf-8",
    )

    loaded = settings_mod.load_env_files_into_environ(force=True)

    assert loaded[-1] == runtime
    assert os.environ["LLM_MODEL"] == "saved-in-app"
    assert os.environ["LLM_REASONING_EFFORT"] == "high"
    assert os.environ["LLM_TEMPERATURE"] == "0.7"  # untouched by the file
    # Overrides are keys that already held a DIFFERENT value; a key the file
    # merely adds is not one.
    assert settings_mod.runtime_settings_overrides() == (
        "LLM_MODEL",
        "LLM_REASONING_EFFORT",
    )


def test_a_value_equal_to_the_environment_is_not_reported_as_an_override(container):
    _app, runtime = container
    os.environ["LLM_MODEL"] = "same"
    runtime.write_text("LLM_MODEL=same\n", encoding="utf-8")

    settings_mod.load_env_files_into_environ(force=True)

    assert settings_mod.runtime_settings_overrides() == ()


def test_another_install_root_never_sees_this_processes_file(container, tmp_path: Path):
    # doctor and the wizard inspect OTHER roots; this process's durable file
    # must not leak into their view or become their write target.
    _app, runtime = container
    other = tmp_path / "other-install"
    other.mkdir()

    assert runtime not in settings_mod.get_env_file_paths(other)
    assert settings_mod.get_env_write_path(other) != runtime


def test_doctors_root_swap_does_not_pull_this_processes_file_in(
    container, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # `doctor --project-root <other>` swaps the PROJECT_ROOT global before it
    # asks for the other install's env files; comparing against the root this
    # process BOOTED with keeps the swap from defeating the isolation.
    _app, runtime = container
    other = tmp_path / "other-install"
    other.mkdir()
    monkeypatch.setattr(settings_mod, "PROJECT_ROOT", other)

    assert runtime not in settings_mod.get_env_file_paths(other)


def test_the_file_never_moves_a_key_the_container_pins(container):
    # #254 review: a saved NYMERIA_DATA_DIR in a DURABLE file would point api
    # and worker at an empty data dir, and no recreate would clear it.
    _app, runtime = container
    os.environ["NYMERIA_DATA_DIR"] = "/data"
    os.environ["POSTGRES_URI"] = "postgresql://compose"
    runtime.write_text(
        "NYMERIA_DATA_DIR=/workspace/elsewhere\n"
        "POSTGRES_URI=postgresql://planted\n"
        "LLM_MODEL=saved-in-app\n",
        encoding="utf-8",
    )

    settings_mod.load_env_files_into_environ(force=True)

    assert os.environ["NYMERIA_DATA_DIR"] == "/data"
    assert os.environ["POSTGRES_URI"] == "postgresql://compose"
    assert os.environ["LLM_MODEL"] == "saved-in-app"
    line = settings_mod.describe_runtime_settings_file()
    assert "Ignored" in line and "NYMERIA_DATA_DIR" in line and "POSTGRES_URI" in line
    assert "planted" not in line


def test_a_compose_empty_default_is_not_reported_as_an_override(container):
    # compose's ${VAR:-} injects "" for every unset optional; saving that key
    # in the app replaces nothing that was set.
    _app, runtime = container
    os.environ["LLM_BASE_URL"] = ""
    runtime.write_text("LLM_BASE_URL=http://proxy:8317\n", encoding="utf-8")

    settings_mod.load_env_files_into_environ(force=True)

    assert os.environ["LLM_BASE_URL"] == "http://proxy:8317"
    assert settings_mod.runtime_settings_overrides() == ()


def test_the_startup_line_names_the_file_and_overridden_keys_never_values(container):
    _app, runtime = container
    os.environ["OPENAI_API_KEY"] = "sk-from-env-docker"
    runtime.write_text("OPENAI_API_KEY=sk-saved-in-app-secret\n", encoding="utf-8")
    settings_mod.load_env_files_into_environ(force=True)

    line = settings_mod.describe_runtime_settings_file()

    assert line is not None
    assert str(runtime) in line
    assert "OPENAI_API_KEY" in line
    assert "sk-" not in line


def test_the_startup_line_is_silent_when_unset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("NYMERIA_SETTINGS_FILE", raising=False)

    assert settings_mod.describe_runtime_settings_file() is None


# --- the file tools refuse it (#254) ------------------------------------------


@pytest.fixture
def runtime_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))
    data = tmp_path / "data"
    data.mkdir()
    runtime = data / "settings.env"
    runtime.write_text("OPENAI_API_KEY=sk-saved-in-app\n", encoding="utf-8")
    monkeypatch.setenv("NYMERIA_SETTINGS_FILE", str(runtime))
    return runtime


def test_file_read_refuses_the_runtime_settings_file(runtime_file: Path):
    from nymeria.tools.filesystem import file_read

    content, _artifact = file_read.func(str(runtime_file))

    assert "sk-saved-in-app" not in content
    assert "saved settings file" in content
    assert "/settings get" in content


def test_file_write_and_edit_refuse_it_and_leave_it_intact(runtime_file: Path):
    from nymeria.tools.file_edit import file_edit
    from nymeria.tools.filesystem import file_write

    write_result = file_write.func(str(runtime_file), "OPENAI_API_KEY=planted\n")
    edit_result = file_edit.func(
        str(runtime_file),
        edits=[{"operation": "replace", "old_text": "sk-saved-in-app", "new_text": "planted"}],
    )

    assert "saved settings file" in str(write_result)
    assert "saved settings file" in str(edit_result)
    assert runtime_file.read_text(encoding="utf-8") == "OPENAI_API_KEY=sk-saved-in-app\n"


def test_a_symlink_to_it_is_refused_too(runtime_file: Path, tmp_path: Path):
    from nymeria.tools.filesystem import file_read

    alias = tmp_path / "innocent.txt"
    alias.symlink_to(runtime_file)

    content, _artifact = file_read.func(str(alias))

    assert "sk-saved-in-app" not in content
    # The check itself resolves, not only file_read's caller: file_list and the
    # other callers hand it paths as given.
    from nymeria.tools.filesystem import secrets_path_error

    assert secrets_path_error(alias) is not None


def test_other_env_files_are_unaffected_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # No shape-named file, no new refusal: this is not a blanket .env block.
    from nymeria.tools.filesystem import secrets_path_error

    monkeypatch.delenv("NYMERIA_SETTINGS_FILE", raising=False)
    other = tmp_path / "settings.env"
    other.write_text("K=V\n", encoding="utf-8")

    assert secrets_path_error(other) is None
