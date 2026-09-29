"""How a running process reads its env files, and when it refuses to (#302).

The files are materialized into ``os.environ`` ONCE, at boot, and ``Settings``
carries no ``env_file``, so a running process is authoritative: nothing re-reads
a config file behind its own back. Before this, the dotenv source sat under the
env source and FILLED GAPS, so any key the process did not hold non-empty was
served from the file on every settings reload, with no graph rebuild and no
``restart_required``. These pin the contract from both sides: what a load does,
and what a reload no longer does.

The loader writes ``os.environ`` directly (that is its whole job), so these rely
on conftest hermeticity layer 3 restoring the environment per test rather than
on monkeypatch.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from nymeria.config import settings as settings_mod
from nymeria.config.settings import Settings, get_settings


@pytest.fixture(autouse=True)
def _isolated_env_loading():
    """Every test starts with no load recorded and no pins registered."""
    settings_mod.reset_env_loading_state_for_tests()
    yield
    settings_mod.reset_env_loading_state_for_tests()


def test_a_file_edit_after_boot_does_not_reach_a_reloaded_settings(tmp_path: Path):
    # The core of #302, and its two halves are the opposite of what you would
    # guess. A key the process holds NON-EMPTY was always shadowed by the
    # environment, so the edit did nothing. A key sitting EMPTY there was the
    # exposed one: `_NonEmptyEnvSource` drops an empty string (what every
    # `${VAR:-}` compose expansion produces) and the dotenv source underneath
    # then served the file's CURRENT value, into the settings object only, with
    # no graph rebuild and no restart_required. Both halves are pinned here.
    (tmp_path / ".env").write_text("LLM_MODEL=booted\n", encoding="utf-8")
    settings_mod.load_env_files_into_environ(tmp_path)
    os.environ["TWITCH_CHANNEL"] = ""
    get_settings.cache_clear()
    assert get_settings().llm_model == "booted"

    (tmp_path / ".env").write_text(
        "LLM_MODEL=edited-behind-our-back\nTWITCH_CHANNEL=edited-behind-our-back\n",
        encoding="utf-8",
    )
    get_settings.cache_clear()

    assert get_settings().llm_model == "booted"
    assert get_settings().twitch_channel != "edited-behind-our-back"


def test_a_key_added_to_the_file_after_boot_stays_out(tmp_path: Path):
    # The gap-fill's widest hole: a key absent from the environment was served
    # straight from the file, so adding a line applied it on the next reload.
    (tmp_path / ".env").write_text("LLM_MODEL=booted\n", encoding="utf-8")
    settings_mod.load_env_files_into_environ(tmp_path)
    os.environ.pop("TWITCH_CHANNEL", None)

    (tmp_path / ".env").write_text(
        "LLM_MODEL=booted\nTWITCH_CHANNEL=added-after-boot\n", encoding="utf-8"
    )
    get_settings.cache_clear()

    assert get_settings().twitch_channel != "added-after-boot"


def test_the_load_happens_once_so_it_cannot_undo_a_runtime_pin(tmp_path: Path):
    # run.py pops REDIS_URL for slim AFTER loading the files, so a second
    # unforced load (get_settings() on any later path) must be a no-op. This is
    # the shape that silently put slim back on the cross-process bus.
    (tmp_path / ".env").write_text(
        "REDIS_URL=redis://from-file:6379/0\n", encoding="utf-8"
    )
    settings_mod.load_env_files_into_environ(tmp_path)
    assert os.environ["REDIS_URL"] == "redis://from-file:6379/0"

    settings_mod.register_runtime_pins({"REDIS_URL": None})
    assert "REDIS_URL" not in os.environ

    assert settings_mod.load_env_files_into_environ(tmp_path) == []
    assert "REDIS_URL" not in os.environ


def test_a_forced_reload_re_reads_the_files_but_re_applies_the_pins(tmp_path: Path):
    # The explicit-reload path: it must pick the file's edits up, and must not
    # let the file reintroduce what the runtime shape deliberately removed.
    (tmp_path / ".env").write_text(
        "LLM_MODEL=booted\nREDIS_URL=redis://from-file:6379/0\n", encoding="utf-8"
    )
    settings_mod.load_env_files_into_environ(tmp_path)
    settings_mod.register_runtime_pins({"REDIS_URL": None})

    (tmp_path / ".env").write_text(
        "LLM_MODEL=edited\nREDIS_URL=redis://from-file:6379/0\n", encoding="utf-8"
    )
    loaded = settings_mod.load_env_files_into_environ(tmp_path, force=True)

    assert loaded == [tmp_path / ".env"]
    assert os.environ["LLM_MODEL"] == "edited"
    assert "REDIS_URL" not in os.environ


def test_a_pin_registered_before_a_load_still_wins(tmp_path: Path):
    # Registration order must not decide the outcome: pins are re-applied after
    # every load, not merely at the moment they are registered.
    settings_mod.register_runtime_pins({"REDIS_URL": None, "DATABASE_BACKEND": "sqlite"})
    (tmp_path / ".env").write_text(
        "REDIS_URL=redis://from-file:6379/0\nDATABASE_BACKEND=postgres\n",
        encoding="utf-8",
    )

    settings_mod.load_env_files_into_environ(tmp_path)

    assert "REDIS_URL" not in os.environ
    assert os.environ["DATABASE_BACKEND"] == "sqlite"


def test_an_unreadable_env_file_is_skipped_rather_than_fatal(tmp_path: Path):
    # A non-UTF-8 byte in a password, or a Notepad UTF-16 BOM, must not stop a
    # process from starting on the rest of its configuration.
    (tmp_path / ".env").write_bytes(b"LLM_MODEL=\xff\xfe-not-utf8\n")
    (tmp_path / "config.env").write_text("LLM_MODEL=from-readable\n", encoding="utf-8")

    loaded = settings_mod.load_env_files_into_environ(tmp_path)

    assert tmp_path / "config.env" in loaded
    assert os.environ["LLM_MODEL"] == "from-readable"


def test_suppression_covers_the_default_chain_only(tmp_path: Path):
    # conftest suppresses the DEFAULT load so no test can pull the checkout's
    # real .env.docker into the process (#294). A caller that names its own root
    # is taking responsibility for what it reads and is still served.
    assert settings_mod.load_env_files_into_environ() == []

    (tmp_path / ".env").write_text("LLM_MODEL=named-root\n", encoding="utf-8")
    assert settings_mod.load_env_files_into_environ(tmp_path) == [tmp_path / ".env"]
    assert os.environ["LLM_MODEL"] == "named-root"


def test_naming_this_checkouts_own_root_is_not_a_suppression_escape(tmp_path: Path):
    # The carve-out above is for a DIFFERENT install, not for a different way of
    # spelling this one. `Settings.project_root` is a property returning the
    # global, so every `settings.project_root` argument in the codebase (the
    # reload endpoint's included) resolves to the default chain by another name;
    # keying suppression on `is None` alone let those pull the operator's real
    # .env.docker into the suite (#294).
    assert settings_mod.load_env_files_into_environ(settings_mod.PROJECT_ROOT) == []
    assert (
        settings_mod.load_env_files_into_environ(
            settings_mod.PROJECT_ROOT, force=True
        )
        == []
    )
    # Still an escape hatch for a root that is genuinely elsewhere.
    (tmp_path / ".env").write_text("LLM_MODEL=elsewhere\n", encoding="utf-8")
    assert settings_mod.load_env_files_into_environ(tmp_path) == [tmp_path / ".env"]


def test_a_process_that_never_ran_run_py_still_reads_its_config_files():
    # `run.py` used to be the only thing that loaded the env files, so anything
    # importing the package directly (the MCP server, a bot entry point, a
    # console script, a test harness) got whatever the shell happened to export
    # and silently ignored the deployment's own config. Dropping `env_file` from
    # `Settings` would have made that permanent, so `get_settings()` performs
    # the load itself.
    #
    # Asserted in a REAL subprocess: the suite deliberately suppresses the
    # default chain (#294), so this is the one thing it cannot observe in
    # process, and a spy on the call would only restate the implementation.
    with tempfile.TemporaryDirectory() as root:
        (Path(root) / ".env").write_text(
            "LLM_MODEL=from-the-deployments-own-file\n", encoding="utf-8"
        )
        env = {
            key: value
            for key, value in os.environ.items()
            if key in ("PATH", "HOME", "PYTHONPATH", "LANG", "VIRTUAL_ENV")
        }
        env["NYMERIA_PROJECT_ROOT"] = root
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from nymeria.config.settings import get_settings;"
                "print(get_settings().llm_model)",
            ],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "from-the-deployments-own-file"


def test_an_explicitly_passed_env_file_is_still_read(tmp_path: Path):
    # The escape hatch `doctor` relies on to inspect a DIFFERENT project root.
    # Dropping `env_file` from model_config must not close it.
    path = tmp_path / "config.env"
    path.write_text("LLM_MODEL=from-explicit-file\n", encoding="utf-8")

    assert Settings(_env_file=str(path)).llm_model == "from-explicit-file"


def test_settings_declares_no_env_file_of_its_own():
    # The mechanism behind every assertion above: if this comes back, the dotenv
    # source starts filling gaps again and the process stops being authoritative.
    assert not Settings.model_config.get("env_file")


# --- #101 entry 6: a Docker install's .env.docker loaded outside Docker ------


def _root_with(tmp_path: Path, monkeypatch, *, local: str | None = None, docker: str | None = None):
    """Load a root's env files as a process there would; not in a container."""
    root = tmp_path / "root"
    root.mkdir()
    if local is not None:
        (root / ".env").write_text(local, encoding="utf-8")
    if docker is not None:
        (root / ".env.docker").write_text(docker, encoding="utf-8")
    monkeypatch.setattr(settings_mod, "_in_container", lambda: False)
    settings_mod.load_env_files_into_environ(root)
    return root


def _docker_env_warnings(*, server_process: bool = True, **settings_kwargs):
    _errors, warnings = Settings(_env_file=None, **settings_kwargs).validate_runtime(
        server_process=server_process
    )
    return [w for w in warnings if "a Docker install's config" in w]


def test_a_docker_config_that_overrides_the_local_one_is_named(tmp_path, monkeypatch):
    root = _root_with(
        tmp_path,
        monkeypatch,
        local="LLM_MODEL=local-model\nLLM_TEMPERATURE=0.5\n",
        docker="LLM_MODEL=docker-model\nLLM_TEMPERATURE=0.5\nONLY_IN_DOCKER=1\n",
    )

    [warning] = _docker_env_warnings()
    assert str(root) in warning
    assert "LLM_MODEL" in warning
    # Equal values are no conflict, and a Docker-only non-URL key is not one.
    assert "LLM_TEMPERATURE" not in warning and "ONLY_IN_DOCKER" not in warning
    assert "local-model" not in warning and "docker-model" not in warning
    assert "NYMERIA_PROJECT_ROOT" in warning


def test_docker_service_hostnames_in_effect_are_named(tmp_path, monkeypatch):
    _root_with(
        tmp_path,
        monkeypatch,
        docker=(
            "LLM_BASE_URL=http://cli-proxy-api-latest:8317/v1\n"
            "NYMERIA_API_URL=http://nymeria-api:8000\n"
            "SEARXNG_BASE_URL=http://localhost:8888\n"
            "OPENROUTER_BASE_URL=https://openrouter.ai/api/v1\n"
            "TTS_BASE_URL=http://172.18.0.5:8880\n"
            "STT_BASE_URL=http://[fd00::5]:8000\n"
            "POSTGRES_URI=postgresql://nymeria:secretpw@postgres:5432/nymeria\n"
        ),
    )

    [warning] = _docker_env_warnings(database_backend="sqlite")
    assert "LLM_BASE_URL (cli-proxy-api-latest)" in warning
    assert "NYMERIA_API_URL (nymeria-api)" in warning
    # Loopback, real domains, and literal IPs resolve outside the stack.
    for key in ("SEARXNG_BASE_URL", "OPENROUTER_BASE_URL", "TTS_BASE_URL", "STT_BASE_URL"):
        assert key not in warning
    # An unused Postgres URI is not a problem; a used one is, credentials never shown.
    assert "POSTGRES_URI" not in warning
    [warning] = _docker_env_warnings(database_backend="postgres", postgres_uri="x")
    assert "POSTGRES_URI (postgres)" in warning
    assert "secretpw" not in warning


def test_a_thin_client_hears_only_about_the_api_url_it_dials(tmp_path, monkeypatch):
    # A host bot pointed at http://nymeria-api:8000 cannot reach the API; the
    # server's LLM route and the local config's overrides are not its concern.
    _root_with(
        tmp_path,
        monkeypatch,
        local="LLM_MODEL=local-model\n",
        docker=(
            "NYMERIA_API_URL=http://nymeria-api:8000\n"
            "LLM_BASE_URL=http://cli-proxy-api-latest:8317/v1\n"
            "LLM_MODEL=docker-model\n"
        ),
    )

    [warning] = _docker_env_warnings(server_process=False)
    assert "NYMERIA_API_URL (nymeria-api)" in warning
    assert "LLM_BASE_URL" not in warning and "LLM_MODEL" not in warning


def test_a_thin_client_is_silent_about_urls_it_never_dials(tmp_path, monkeypatch):
    _root_with(
        tmp_path, monkeypatch, docker="LLM_BASE_URL=http://cli-proxy-api-latest:8317/v1\n"
    )

    assert _docker_env_warnings(server_process=False) == []
    assert _docker_env_warnings() != []  # ...which the server still hears about


def test_a_value_changed_after_loading_is_not_blamed_on_the_file(tmp_path, monkeypatch):
    _root_with(tmp_path, monkeypatch, docker="LLM_BASE_URL=http://cli-proxy-api:8317\n")
    os.environ["LLM_BASE_URL"] = "http://localhost:8318"

    assert _docker_env_warnings() == []


def test_silent_when_the_docker_config_is_clean_absent_or_in_a_container(tmp_path, monkeypatch):
    _root_with(tmp_path, monkeypatch, docker="LLM_MODEL=only-here\n")
    assert _docker_env_warnings() == []

    settings_mod.reset_env_loading_state_for_tests()
    other = tmp_path / "other"
    other.mkdir()
    (other / ".env").write_text("LLM_MODEL=local\n", encoding="utf-8")
    settings_mod.load_env_files_into_environ(other)
    assert _docker_env_warnings() == []

    settings_mod.reset_env_loading_state_for_tests()
    inside = tmp_path / "inside"
    inside.mkdir()
    (inside / ".env.docker").write_text("NYMERIA_API_URL=http://nymeria-api:8000\n", encoding="utf-8")
    monkeypatch.setattr(settings_mod, "_in_container", lambda: True)
    settings_mod.load_env_files_into_environ(inside)
    assert _docker_env_warnings() == []


def test_env_file_source_names_the_last_loaded_file_holding_that_value(tmp_path, monkeypatch):
    first = tmp_path / ".env"
    second = tmp_path / ".env.docker"
    first.write_text("TOKEN=a\nOTHER=x\nSAME=v\n", encoding="utf-8")
    second.write_text("TOKEN=b\nSAME=v\n", encoding="utf-8")
    monkeypatch.setattr(settings_mod, "_loaded_env_files", (first, second))

    assert settings_mod.env_file_source("TOKEN", "b") == second
    assert settings_mod.env_file_source("TOKEN", "a") == first  # overridden since, but its value
    assert settings_mod.env_file_source("OTHER", "x") == first
    assert settings_mod.env_file_source("TOKEN", "shell") is None  # exported, not from a file
    assert settings_mod.env_file_source("MISSING", "x") is None
    assert settings_mod.env_file_source("SAME", "v") == second  # the one that loaded last
