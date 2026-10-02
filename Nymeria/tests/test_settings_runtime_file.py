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
    # #101 entry 19: compose derives it from the port mapping expression.
    os.environ["NYMERIA_PUBLISHED_API_PORT"] = "8020"
    runtime.write_text(
        "NYMERIA_DATA_DIR=/workspace/elsewhere\n"
        "POSTGRES_URI=postgresql://planted\n"
        "NYMERIA_PUBLISHED_API_PORT=8000\n"
        "LLM_MODEL=saved-in-app\n",
        encoding="utf-8",
    )

    settings_mod.load_env_files_into_environ(force=True)

    assert os.environ["NYMERIA_DATA_DIR"] == "/data"
    assert os.environ["POSTGRES_URI"] == "postgresql://compose"
    assert os.environ["NYMERIA_PUBLISHED_API_PORT"] == "8020"
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


# --- #434: the report is live, classed, and judged against the boot baseline --
#
# Every value below is a dummy whose substring "dummy-434" must never reach a
# report, a log line, or a response: the report carries key NAMES only.

DUMMY = "dummy-434"


def _shadow_map() -> dict[str, tuple[str, str]]:
    return {
        shadow.key: (shadow.key_class, shadow.kind)
        for shadow in settings_mod.runtime_settings_shadows()
    }


def test_a_shadow_is_classed_credential_route_or_setting(container):
    _app, runtime = container
    os.environ["OPENAI_API_KEY"] = f"cpx-{DUMMY}-gatekeeper"
    os.environ["LLM_BASE_URL"] = f"http://proxy-{DUMMY}:8317/v1"
    os.environ["USER_TIMEZONE"] = "UTC"
    runtime.write_text(
        f"OPENAI_API_KEY=sk-{DUMMY}-old\n"
        f"LLM_BASE_URL=https://api.openai.com/v1\n"
        "USER_TIMEZONE=Australia/Sydney\n",
        encoding="utf-8",
    )

    settings_mod.load_env_files_into_environ(force=True)

    assert _shadow_map() == {
        "LLM_BASE_URL": ("route", "override"),
        "OPENAI_API_KEY": ("credential", "override"),
        "USER_TIMEZONE": ("setting", "override"),
    }
    assert settings_mod.runtime_settings_overrides() == (
        "LLM_BASE_URL",
        "OPENAI_API_KEY",
        "USER_TIMEZONE",
    )


def test_an_equal_value_is_not_a_shadow(container):
    _app, runtime = container
    os.environ["OPENAI_API_KEY"] = f"sk-{DUMMY}"
    runtime.write_text(f"OPENAI_API_KEY=sk-{DUMMY}\n", encoding="utf-8")

    settings_mod.load_env_files_into_environ(force=True)

    assert settings_mod.runtime_settings_shadows() == ()


def test_a_key_set_nowhere_else_is_saved_only_in_the_app_never_an_override(container):
    # G1 stays informational: a GUI-onboarded Docker install legitimately
    # holds its whole route here, and the API cannot tell that apart from a
    # value the wizard dropped.
    _app, runtime = container
    os.environ["LLM_BASE_URL"] = ""  # compose's ${VAR:-}
    os.environ.pop("OPENAI_API_KEY", None)
    runtime.write_text(
        f"LLM_BASE_URL=http://proxy-{DUMMY}:8317\nOPENAI_API_KEY=cpx-{DUMMY}\n",
        encoding="utf-8",
    )

    settings_mod.load_env_files_into_environ(force=True)

    assert _shadow_map() == {
        "LLM_BASE_URL": ("route", "app_only"),
        "OPENAI_API_KEY": ("credential", "app_only"),
    }
    assert settings_mod.runtime_settings_overrides() == ()
    assert settings_mod.describe_runtime_settings_warning() is None


def test_a_reload_does_not_empty_the_report(container):
    # G2: by the second load os.environ already holds the file's values, so a
    # snapshot taken then compares the file against itself.
    _app, runtime = container
    os.environ["OPENAI_API_KEY"] = f"cpx-{DUMMY}"
    runtime.write_text(f"OPENAI_API_KEY=sk-{DUMMY}\n", encoding="utf-8")
    settings_mod.load_env_files_into_environ(force=True)
    assert settings_mod.runtime_settings_overrides() == ("OPENAI_API_KEY",)

    settings_mod.load_env_files_into_environ(force=True)  # POST /settings/reload

    assert settings_mod.runtime_settings_overrides() == ("OPENAI_API_KEY",)
    assert settings_mod.runtime_settings_baseline("OPENAI_API_KEY") == f"cpx-{DUMMY}"


def test_a_value_saved_after_boot_is_reported_without_a_restart(container):
    # G3: an in-app save writes the file and os.environ, nothing else.
    from nymeria.config.env_file import write_env_file

    _app, runtime = container
    os.environ["LLM_MODEL"] = "from-env-docker"
    settings_mod.load_env_files_into_environ(force=True)  # fresh volume: no file yet
    assert settings_mod.runtime_settings_overrides() == ()

    write_env_file(runtime, [("LLM_MODEL", "saved-in-app")], merge=True)
    os.environ["LLM_MODEL"] = "saved-in-app"

    assert settings_mod.runtime_settings_overrides() == ("LLM_MODEL",)


def test_an_empty_line_over_a_set_value_is_reported_as_blanked(container):
    # G4: `KEY=` masks the value set elsewhere rather than restoring it.
    _app, runtime = container
    os.environ["TTS_BASE_URL"] = "http://tts.local"
    os.environ.pop("STT_BASE_URL", None)
    runtime.write_text("TTS_BASE_URL=\nSTT_BASE_URL=\n", encoding="utf-8")

    settings_mod.load_env_files_into_environ(force=True)

    # Over an unset key an empty line changes nothing, so it is not listed.
    # (TTS_BASE_URL decides where the TTS key goes, so it is a route key.)
    assert _shadow_map() == {"TTS_BASE_URL": ("route", "blanked")}
    assert settings_mod.runtime_settings_overrides() == ("TTS_BASE_URL",)
    assert "TTS_BASE_URL (route, saved empty)" in (
        settings_mod.describe_runtime_settings_file() or ""
    )


def test_a_root_env_file_counts_as_set_elsewhere(container):
    # The baseline is the boot env overlaid by the root files of THIS load.
    app, runtime = container
    (app / "config.env").write_text("LLM_MODEL=from-root-file\n", encoding="utf-8")
    runtime.write_text("LLM_MODEL=saved-in-app\n", encoding="utf-8")

    settings_mod.load_env_files_into_environ(force=True)
    settings_mod.load_env_files_into_environ(force=True)

    assert settings_mod.runtime_settings_overrides() == ("LLM_MODEL",)
    assert settings_mod.runtime_settings_baseline("LLM_MODEL") == "from-root-file"


def test_the_warning_line_names_credential_and_route_keys_and_the_remedy(container):
    _app, runtime = container
    os.environ["OPENAI_API_KEY"] = f"cpx-{DUMMY}-gatekeeper"
    os.environ["LLM_BASE_URL"] = f"http://proxy-{DUMMY}:8317/v1"
    os.environ["USER_TIMEZONE"] = "UTC"
    runtime.write_text(
        f"OPENAI_API_KEY=sk-{DUMMY}-old\nLLM_BASE_URL=https://{DUMMY}.example/v1\n"
        "USER_TIMEZONE=Australia/Sydney\n",
        encoding="utf-8",
    )
    settings_mod.load_env_files_into_environ(force=True)

    warning = settings_mod.describe_runtime_settings_warning()
    info = settings_mod.describe_runtime_settings_file()

    assert warning is not None and info is not None
    assert "OPENAI_API_KEY (credential)" in warning
    assert "LLM_BASE_URL (route)" in warning
    assert "USER_TIMEZONE" not in warning  # a plain setting stays on the INFO line
    assert "/settings clear <KEY>" in warning
    assert "OPENAI_DIRECT_API_KEY" in warning  # where a real key goes first
    assert "USER_TIMEZONE" in info and "/settings clear <KEY>" in info
    for line in (warning, info):
        assert DUMMY not in line and "sk-" not in line and "cpx-" not in line


def test_only_plain_setting_overrides_raise_no_warning(container):
    _app, runtime = container
    os.environ["USER_TIMEZONE"] = "UTC"
    runtime.write_text("USER_TIMEZONE=Australia/Sydney\n", encoding="utf-8")
    settings_mod.load_env_files_into_environ(force=True)

    assert settings_mod.describe_runtime_settings_warning() is None
    assert "USER_TIMEZONE" in (settings_mod.describe_runtime_settings_file() or "")


def test_a_restart_carries_the_baseline_not_the_files_values(container):
    # /restart api re-execs with this process's environment. Carrying the
    # file's values across would make the file its own baseline in the new
    # image: every shadow gone from the report, and a clear a no-op.
    from nymeria.api.routers.system import _restart_child_env

    app, runtime = container
    os.environ["OPENAI_API_KEY"] = f"cpx-{DUMMY}"
    os.environ.pop("TWITCH_CHANNEL", None)
    os.environ["UNRELATED_PROCESS_VAR"] = "kept"
    runtime.write_text(
        f"OPENAI_API_KEY=sk-{DUMMY}\nTWITCH_CHANNEL=saved-in-app\n", encoding="utf-8"
    )
    settings_mod.load_env_files_into_environ(force=True)
    assert os.environ["OPENAI_API_KEY"] == f"sk-{DUMMY}"

    class _Settings:
        project_root = app

    env = _restart_child_env(_Settings())

    assert env["OPENAI_API_KEY"] == f"cpx-{DUMMY}"
    assert "TWITCH_CHANNEL" not in env
    assert env["UNRELATED_PROCESS_VAR"] == "kept"
    # The running process itself is untouched by building the child's env.
    assert os.environ["OPENAI_API_KEY"] == f"sk-{DUMMY}"


def test_a_restart_applies_a_line_removed_by_hand(container):
    # The restart env puts back every key the LAST load applied, not only the
    # keys the file holds now: a line the operator deleted on the host after
    # boot must not ride the exec into the new image (documented in api.md).
    from nymeria.api.routers.system import _restart_child_env

    app, runtime = container
    os.environ["LLM_MODEL"] = "from-env-docker"
    os.environ.pop("TWITCH_CHANNEL", None)
    runtime.write_text("LLM_MODEL=saved-in-app\nTWITCH_CHANNEL=saved-in-app\n", encoding="utf-8")
    settings_mod.load_env_files_into_environ(force=True)
    runtime.write_text("", encoding="utf-8")  # both lines removed by hand

    class _Settings:
        project_root = app

    env = _restart_child_env(_Settings())

    assert env["LLM_MODEL"] == "from-env-docker"
    assert "TWITCH_CHANNEL" not in env


@pytest.mark.parametrize(
    "key", ["LLM_BACKGROUND_BASE_URL", "EMBEDDING_BASE_URL", "TTS_BASE_URL", "STT_BASE_URL"]
)
def test_every_base_url_that_carries_a_provider_key_is_a_route(container, key):
    # A stale copy of any of these sends that provider's key to the old host,
    # so it warns like LLM_BASE_URL does.
    _app, runtime = container
    os.environ[key] = f"http://proxy-{DUMMY}:8317/v1"
    runtime.write_text(f"{key}=https://{DUMMY}.example/v1\n", encoding="utf-8")

    settings_mod.load_env_files_into_environ(force=True)

    assert _shadow_map() == {key: ("route", "override")}
    warning = settings_mod.describe_runtime_settings_warning() or ""
    assert f"{key} (route)" in warning
    assert DUMMY not in warning


def test_a_shadowed_anthropic_key_names_its_direct_slot(container):
    _app, runtime = container
    os.environ["ANTHROPIC_API_KEY"] = f"cpx-{DUMMY}"
    runtime.write_text(f"ANTHROPIC_API_KEY=sk-ant-{DUMMY}\n", encoding="utf-8")

    settings_mod.load_env_files_into_environ(force=True)

    warning = settings_mod.describe_runtime_settings_warning() or ""
    assert "ANTHROPIC_API_KEY (credential)" in warning
    assert "save it as ANTHROPIC_DIRECT_API_KEY first" in warning
    assert DUMMY not in warning


# --- #435: the report and the removal the wizard runs in the container ---------


def _report_map() -> dict[str, dict]:
    return {
        entry["key"]: {k: v for k, v in entry.items() if k != "key"}
        for entry in settings_mod.runtime_settings_file_report()["keys"]
    }


def test_the_report_lists_every_key_with_kind_class_and_shape(container):
    # Unlike the shadow report it keeps `same` copies (a copy equal to the
    # OLD .env.docker value still beats a new one after a recreate) and
    # blank lines over nothing; pinned keys never apply, so never listed.
    _app, runtime = container
    os.environ["LLM_PROVIDER"] = "openai"
    os.environ["OPENAI_API_KEY"] = f"sk-{DUMMY}-env"
    os.environ["USER_TIMEZONE"] = "UTC"
    os.environ["LLM_MODEL"] = "gpt-env"
    for absent in (
        "GEMINI_API_KEY", "GEMINI_DIRECT_API_KEY", "OPENAI_DIRECT_API_KEY",
        "STT_PROVIDER", "LLM_BASE_URL",
    ):
        os.environ.pop(absent, None)
    runtime.write_text(
        f"OPENAI_API_KEY=sk-{DUMMY}-app\n"
        "USER_TIMEZONE=UTC\n"
        "LLM_MODEL=\n"
        "STT_PROVIDER=\n"
        "LLM_BASE_URL=http://litellm.example:4000/v1\n"
        f"GEMINI_API_KEY=AIza-{DUMMY}\n"
        "NYMERIA_DATA_DIR=/elsewhere\n",
        encoding="utf-8",
    )

    settings_mod.load_env_files_into_environ(force=True)
    report = settings_mod.runtime_settings_file_report()

    assert report["file"] is True
    assert _report_map() == {
        # Judged under the APP's effective route: the file's LiteLLM base URL
        # with the environment's openai provider feeds the slot to a gateway.
        "OPENAI_API_KEY": {
            "class": "credential", "kind": "override", "empty": False,
            "shape": "gateway", "direct_set": False,
        },
        "USER_TIMEZONE": {"class": "setting", "kind": "same", "empty": False},
        "LLM_MODEL": {"class": "setting", "kind": "blanked", "empty": True},
        "STT_PROVIDER": {"class": "setting", "kind": "same", "empty": True},
        "LLM_BASE_URL": {"class": "route", "kind": "app_only", "empty": False},
        # A google route never reads it: the vendor's own key.
        "GEMINI_API_KEY": {
            "class": "credential", "kind": "app_only", "empty": False,
            "shape": "vendor", "direct_set": False,
        },
    }
    assert DUMMY not in str(report)


@pytest.mark.parametrize(
    "file_line,env,shape,direct_set",
    [
        # Under a route that keeps the slot the vendor's: a real key.
        (f"OPENAI_API_KEY=sk-{DUMMY}", {"LLM_PROVIDER": "anthropic"}, "vendor", False),
        (f"OPENAI_API_KEY=cpx-{DUMMY}", {"LLM_PROVIDER": "openai"}, "gatekeeper", False),
        # A non-vendor-shaped value off a gateway route is neither.
        (f"OPENAI_API_KEY=lm-{DUMMY}", {"LLM_PROVIDER": "anthropic"}, None, False),
        # The direct slot set in the environment only.
        (
            f"OPENAI_API_KEY=sk-{DUMMY}",
            {"LLM_PROVIDER": "anthropic", "OPENAI_DIRECT_API_KEY": f"sk-{DUMMY}-d"},
            "vendor",
            True,
        ),
    ],
)
def test_the_reported_shape_never_carries_the_value(container, file_line, env, shape, direct_set):
    _app, runtime = container
    for key in ("LLM_BASE_URL", "OPENAI_DIRECT_API_KEY", "LLM_PROVIDER"):
        os.environ.pop(key, None)
    os.environ.update(env)
    runtime.write_text(file_line + "\n", encoding="utf-8")

    settings_mod.load_env_files_into_environ(force=True)

    entry = _report_map()["OPENAI_API_KEY"]
    assert (entry["shape"], entry["direct_set"]) == (shape, direct_set)
    assert DUMMY not in str(settings_mod.runtime_settings_file_report())


def test_the_shape_follows_the_files_route_without_a_reload(container):
    # The file's route wins where it sets one, read from the file itself (as
    # the kinds are), not from whatever this process last exported: an
    # in-app save of a gateway base URL after the load makes the same key a
    # gateway's key.
    _app, runtime = container
    for key in ("LLM_BASE_URL", "OPENAI_DIRECT_API_KEY"):
        os.environ.pop(key, None)
    os.environ["LLM_PROVIDER"] = "openai"
    runtime.write_text(f"OPENAI_API_KEY=sk-{DUMMY}\n", encoding="utf-8")
    settings_mod.load_env_files_into_environ(force=True)
    assert _report_map()["OPENAI_API_KEY"]["shape"] == "vendor"

    runtime.write_text(
        f"OPENAI_API_KEY=sk-{DUMMY}\nLLM_BASE_URL=http://litellm.example:4000/v1\n",
        encoding="utf-8",
    )

    assert _report_map()["OPENAI_API_KEY"]["shape"] == "gateway"


def test_the_report_without_a_runtime_file_says_so(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("NYMERIA_SETTINGS_FILE", raising=False)
    assert settings_mod.runtime_settings_file_report() == {"file": False, "keys": []}


def test_removal_drops_every_line_for_each_key_and_keeps_the_rest(container):
    _app, runtime = container
    runtime.write_text(
        "# saved in the app\n"
        f"OPENAI_API_KEY=sk-{DUMMY}-a\n"
        "USER_TIMEZONE=UTC\n"
        f"  OPENAI_API_KEY = sk-{DUMMY}-b\n"
        "LLM_BASE_URL=http://x:9/v1\n"
        "NYMERIA_DATA_DIR=/elsewhere\n",
        encoding="utf-8",
    )
    runtime.chmod(0o644)

    status = settings_mod.remove_runtime_settings_keys(
        ["OPENAI_API_KEY", "LLM_BASE_URL", "NYMERIA_DATA_DIR", "LLM_MODEL"]
    )

    assert status == {
        "OPENAI_API_KEY": "cleared",
        "LLM_BASE_URL": "cleared",
        "NYMERIA_DATA_DIR": "pinned_line_removed",
        "LLM_MODEL": "not_saved",
    }
    assert runtime.read_text(encoding="utf-8") == "# saved in the app\nUSER_TIMEZONE=UTC\n"
    assert runtime.stat().st_mode & 0o777 == 0o600


def test_a_relocation_moves_the_value_and_drops_the_slot_in_one_write(container, monkeypatch):
    _app, runtime = container
    os.environ["LLM_PROVIDER"] = "anthropic"
    os.environ.pop("OPENAI_DIRECT_API_KEY", None)
    os.environ.pop("LLM_BASE_URL", None)
    value = f"sk-{DUMMY} with space"
    runtime.write_text(f'# keep\nOPENAI_API_KEY="{value}"\n', encoding="utf-8")
    from nymeria.config import env_file

    writes: list[tuple] = []
    real_write = env_file.write_env_file
    monkeypatch.setattr(
        env_file,
        "write_env_file",
        lambda *a, **kw: writes.append((a, kw)) or real_write(*a, **kw),
    )

    status = settings_mod.remove_runtime_settings_keys(
        [], relocate={"OPENAI_API_KEY": "OPENAI_DIRECT_API_KEY"}
    )

    assert status == {"OPENAI_API_KEY": "relocated"}
    assert len(writes) == 1
    from dotenv import dotenv_values

    assert dotenv_values(runtime) == {"OPENAI_DIRECT_API_KEY": value}
    assert runtime.read_text(encoding="utf-8").startswith("# keep\n")


@pytest.mark.parametrize(
    "file_text,env",
    [
        # The direct slot was saved since the probe.
        (f"OPENAI_API_KEY=sk-{DUMMY}\nOPENAI_DIRECT_API_KEY=sk-{DUMMY}-d\n", {}),
        # ...or is set in the environment.
        (f"OPENAI_API_KEY=sk-{DUMMY}\n", {"OPENAI_DIRECT_API_KEY": f"sk-{DUMMY}-d"}),
        # The app's own route now feeds the slot to a gateway: not the vendor's.
        (f"OPENAI_API_KEY=sk-{DUMMY}\nLLM_BASE_URL=http://litellm:4000/v1\n", {}),
        # Not vendor-shaped.
        (f"OPENAI_API_KEY=cpx-{DUMMY}\n", {}),
    ],
)
def test_a_relocation_that_no_longer_qualifies_keeps_the_slot(container, file_text, env):
    _app, runtime = container
    os.environ["LLM_PROVIDER"] = "openai"
    os.environ.pop("OPENAI_DIRECT_API_KEY", None)
    os.environ.pop("LLM_BASE_URL", None)
    os.environ.update(env)
    runtime.write_text(file_text, encoding="utf-8")

    status = settings_mod.remove_runtime_settings_keys(
        ["OPENAI_API_KEY"], relocate={"OPENAI_API_KEY": "OPENAI_DIRECT_API_KEY"}
    )

    assert status == {"OPENAI_API_KEY": "relocation_refused"}
    assert runtime.read_text(encoding="utf-8") == file_text


def test_an_unparsable_line_is_never_claimed_removed(container):
    _app, runtime = container
    text = f"export OPENAI_API_KEY=sk-{DUMMY}\nLLM_BASE_URL=http://x:9/v1\n"
    runtime.write_text(text, encoding="utf-8")

    status = settings_mod.remove_runtime_settings_keys(
        ["OPENAI_API_KEY", "LLM_BASE_URL"],
        relocate={"OPENAI_API_KEY": "OPENAI_DIRECT_API_KEY"},
    )

    assert status == {"OPENAI_API_KEY": "unparsable", "LLM_BASE_URL": "cleared"}
    # Nothing moved for the key that stayed, and its line is untouched.
    assert runtime.read_text(encoding="utf-8") == f"export OPENAI_API_KEY=sk-{DUMMY}\n"


def test_a_second_unparsable_line_beside_a_parsable_one_is_reported(container):
    # The writer drops the parsable line; the export one still sets the key.
    _app, runtime = container
    runtime.write_text(
        f"OPENAI_API_KEY=sk-{DUMMY}-a\nexport OPENAI_API_KEY=sk-{DUMMY}-b\n",
        encoding="utf-8",
    )

    status = settings_mod.remove_runtime_settings_keys(["OPENAI_API_KEY"])

    assert status == {"OPENAI_API_KEY": "unparsable"}
    assert runtime.read_text(encoding="utf-8") == f"export OPENAI_API_KEY=sk-{DUMMY}-b\n"


@pytest.mark.parametrize(
    "keys,relocate",
    [
        (["openai_api_key"], {}),
        (["KEY; rm -rf /"], {}),
        ([], {"OPENAI_API_KEY": "ANTHROPIC_DIRECT_API_KEY"}),  # not its twin
        ([], {"LLM_BASE_URL": "OPENAI_DIRECT_API_KEY"}),  # not a shared slot
    ],
)
def test_malformed_names_and_pairs_change_nothing(container, keys, relocate):
    _app, runtime = container
    text = "OPENAI_API_KEY=sk-x\nLLM_BASE_URL=http://x:9/v1\nopenai_api_key=y\n"
    runtime.write_text(text, encoding="utf-8")

    status = settings_mod.remove_runtime_settings_keys(keys, relocate=relocate)

    assert set(status.values()) == {"malformed"}
    assert runtime.read_text(encoding="utf-8") == text


def test_removal_without_a_file_reports_not_saved(container, monkeypatch):
    _app, runtime = container
    assert not runtime.exists()
    assert settings_mod.remove_runtime_settings_keys(["LLM_BASE_URL"]) == {
        "LLM_BASE_URL": "not_saved"
    }
    assert not runtime.exists()
    monkeypatch.delenv("NYMERIA_SETTINGS_FILE")
    assert settings_mod.remove_runtime_settings_keys(["LLM_BASE_URL"]) == {
        "LLM_BASE_URL": "not_saved"
    }


# --- #435 review: judged under the route the app saved its copy beside -------
#
# The wizard's post-start check runs in a container already recreated on the
# NEW route; the route the app saved its copy under travels as booleans.

_NEW_GATEWAY_ENV = {"LLM_PROVIDER": "openai", "LLM_BASE_URL": "http://litellm:4000/v1"}


def _facts(provider, base_url):
    from nymeria.config.vendor_keys import RouteFacts, UNKNOWN_ROUTE

    return {"OPENAI_API_KEY": RouteFacts(provider, base_url), "GEMINI_API_KEY": UNKNOWN_ROUTE}


@pytest.mark.parametrize(
    "file_text,prior,shape",
    [
        # Saved under an anthropic route: the vendor's key, whatever the
        # container's new gateway route says (the review's HIGH).
        (f"OPENAI_API_KEY=sk-{DUMMY}\n", (False, False), "vendor"),
        # One known False decides; the other part need not be known.
        (f"OPENAI_API_KEY=sk-{DUMMY}\n", (False, None), "vendor"),
        (f"OPENAI_API_KEY=sk-{DUMMY}\n", (None, False), "vendor"),
        # Saved under a gateway route: the gateway's key.
        (f"OPENAI_API_KEY=sk-{DUMMY}\n", (True, True), "gateway"),
        # The old route cannot be judged: maybe the vendor's, maybe not.
        (f"OPENAI_API_KEY=sk-{DUMMY}\n", (True, None), "unknown"),
        (f"OPENAI_API_KEY=sk-{DUMMY}\n", (None, None), "unknown"),
        # Not vendor-shaped: never the vendor's key, unknown route or not.
        (f"OPENAI_API_KEY=lm-{DUMMY}\n", (None, None), None),
        (f"OPENAI_API_KEY=cpx-{DUMMY}\n", (None, None), "gatekeeper"),
        # The file's own route wins where it sets it, part by part.
        (
            f"OPENAI_API_KEY=sk-{DUMMY}\nLLM_PROVIDER=openai\n"
            "LLM_BASE_URL=http://litellm.example:4000/v1\n",
            (False, False),
            "gateway",
        ),
        (f"OPENAI_API_KEY=sk-{DUMMY}\nLLM_BASE_URL=http://gw.example/v1\n", (True, False), "gateway"),
        (f"OPENAI_API_KEY=sk-{DUMMY}\nLLM_BASE_URL=http://gw.example/v1\n", (None, False), "unknown"),
        (f"OPENAI_API_KEY=sk-{DUMMY}\nLLM_PROVIDER=anthropic\n", (None, None), "vendor"),
    ],
)
def test_the_shape_is_judged_under_the_route_the_copy_was_saved_beside(
    container, file_text, prior, shape
):
    _app, runtime = container
    os.environ.pop("OPENAI_DIRECT_API_KEY", None)
    os.environ.update(_NEW_GATEWAY_ENV)
    runtime.write_text(file_text, encoding="utf-8")
    settings_mod.load_env_files_into_environ(force=True)

    report = settings_mod.runtime_settings_file_report(prior_route=_facts(*prior))

    entry = next(e for e in report["keys"] if e["key"] == "OPENAI_API_KEY")
    assert entry["shape"] == shape
    assert DUMMY not in str(report)


def test_without_the_old_route_the_shape_follows_this_process(container):
    # The pre-start probe passes no facts: the container still runs the old
    # route, so its own environment is the truth (slice 1 unchanged).
    _app, runtime = container
    os.environ.pop("OPENAI_DIRECT_API_KEY", None)
    os.environ.update(_NEW_GATEWAY_ENV)
    runtime.write_text(f"OPENAI_API_KEY=sk-{DUMMY}\n", encoding="utf-8")
    settings_mod.load_env_files_into_environ(force=True)

    assert _report_map()["OPENAI_API_KEY"]["shape"] == "gateway"


@pytest.mark.parametrize(
    "prior,status",
    [((False, False), "relocated"), ((True, None), "relocation_refused")],
)
def test_the_relocation_re_check_uses_the_same_old_route(container, prior, status):
    # The apply re-check judges as the report did: under the container's new
    # gateway route alone the move was always refused (the review's
    # corollary), and an unknown route never moves a key.
    _app, runtime = container
    os.environ.pop("OPENAI_DIRECT_API_KEY", None)
    os.environ.update(_NEW_GATEWAY_ENV)
    text = f"# keep\nOPENAI_API_KEY=sk-{DUMMY}\n"
    runtime.write_text(text, encoding="utf-8")

    result = settings_mod.remove_runtime_settings_keys(
        [], relocate={"OPENAI_API_KEY": "OPENAI_DIRECT_API_KEY"}, prior_route=_facts(*prior)
    )

    assert result == {"OPENAI_API_KEY": status}
    from dotenv import dotenv_values

    if status == "relocated":
        assert dotenv_values(runtime) == {"OPENAI_DIRECT_API_KEY": f"sk-{DUMMY}"}
    else:
        assert runtime.read_text(encoding="utf-8") == text


def test_an_unreadable_file_raises_the_read_error_and_writes_nothing(container):
    # The #434 route answers this 400 (it was before the shared primitive).
    _app, runtime = container
    runtime.write_bytes(b"OPENAI_API_KEY=\xff\xfe\n")
    before = runtime.read_bytes()

    with pytest.raises(settings_mod.RuntimeSettingsReadError):
        settings_mod.remove_runtime_settings_keys(["OPENAI_API_KEY"])

    assert runtime.read_bytes() == before


def test_a_read_failure_after_the_write_is_not_the_nothing_written_error(container, monkeypatch):
    # Delta review finding 2: the re-read that checks for a leftover line runs
    # AFTER the replace, so the key is already gone. The read error means
    # "nothing was written" to the #434 route; this one must not say that.
    _app, runtime = container
    runtime.write_text(f"# keep\nOPENAI_API_KEY=sk-{DUMMY}\nUSER_TIMEZONE=UTC\n", encoding="utf-8")
    real_read_text = Path.read_text

    def unreadable_once_written(self, *args, **kwargs):
        if self == runtime and b"OPENAI_API_KEY" not in self.read_bytes():
            raise PermissionError("denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable_once_written)

    with pytest.raises(OSError) as raised:
        settings_mod.remove_runtime_settings_keys(["OPENAI_API_KEY"])

    assert not isinstance(raised.value, settings_mod.RuntimeSettingsReadError)
    assert runtime.read_bytes() == b"# keep\nUSER_TIMEZONE=UTC\n"
