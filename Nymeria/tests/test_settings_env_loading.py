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
