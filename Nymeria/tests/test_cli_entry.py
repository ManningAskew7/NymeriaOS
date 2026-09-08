from pathlib import Path

import pytest

from nymeria import _runtime_paths
from nymeria.config import settings as settings_module


def test_runtime_bootstrap_uses_source_checkout_when_markers_exist(
    monkeypatch,
    tmp_path: Path,
):
    root = tmp_path / "Nymeria"
    config_dir = root / "nymeria" / "config"
    config_dir.mkdir(parents=True)
    (root / "run.py").write_text("", encoding="utf-8")
    (config_dir / "soul.md").write_text("", encoding="utf-8")

    monkeypatch.delenv("NYMERIA_PROJECT_ROOT", raising=False)

    resolved = _runtime_paths.configure_project_root(root / "nymeria" / "cli_entry.py")

    assert resolved == root.resolve()


def test_runtime_bootstrap_uses_user_root_for_installed_package(
    monkeypatch,
    tmp_path: Path,
):
    # Reproduce the real wheel layout: run.py ships as a top-level module
    # beside nymeria/ inside site-packages, so the source-checkout markers
    # DO match here. Without the installed-location guard this would strand
    # config/data in site-packages (lost on upgrade); the guard must win.
    site_packages = tmp_path / "site-packages"
    config_dir = site_packages / "nymeria" / "config"
    config_dir.mkdir(parents=True)
    (site_packages / "run.py").write_text("", encoding="utf-8")
    (config_dir / "soul.md").write_text("", encoding="utf-8")
    fake_entry = site_packages / "nymeria" / "cli_entry.py"
    fake_entry.write_text("", encoding="utf-8")
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))  # POSIX
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows
    monkeypatch.delenv("NYMERIA_PROJECT_ROOT", raising=False)

    resolved = _runtime_paths.configure_project_root(fake_entry)

    assert resolved == (home / ".nymeria").resolve()


def test_runtime_bootstrap_uses_user_root_for_frozen_build(
    monkeypatch,
    tmp_path: Path,
):
    fake_meipass_entry = tmp_path / "_MEIPASS" / "nymeria" / "_runtime_paths.py"
    fake_config_dir = tmp_path / "_MEIPASS" / "nymeria" / "config"
    fake_config_dir.mkdir(parents=True)
    (tmp_path / "_MEIPASS" / "run.py").write_text("", encoding="utf-8")
    (fake_config_dir / "soul.md").write_text("", encoding="utf-8")
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))  # POSIX
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows
    monkeypatch.delenv("NYMERIA_PROJECT_ROOT", raising=False)
    monkeypatch.setattr(_runtime_paths.sys, "frozen", True, raising=False)

    resolved = _runtime_paths.configure_project_root(fake_meipass_entry)

    assert resolved == (home / ".nymeria").resolve()


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/home/u/.local/pipx/venvs/nymeriaos/lib/python3.12/site-packages/nymeria/x.py", True),
        ("/usr/lib/python3/dist-packages/nymeria/x.py", True),
        ("/opt/NymeriaOS/Nymeria/nymeria/x.py", False),
        ("/home/u/dev/checkout/nymeria/x.py", False),
    ],
)
def test_is_installed_location(path: str, expected: bool):
    assert _runtime_paths.is_installed_location(Path(path)) is expected


def test_settings_package_paths_do_not_follow_runtime_project_root(
    monkeypatch,
    tmp_path: Path,
):
    runtime_root = tmp_path / "runtime"
    package_root = tmp_path / "site-packages" / "nymeria"
    monkeypatch.delenv("NYMERIA_DATA_DIR", raising=False)
    monkeypatch.delenv("SQLITE_PATH", raising=False)
    monkeypatch.setattr(settings_module, "PROJECT_ROOT", runtime_root)
    monkeypatch.setattr(settings_module, "PACKAGE_ROOT", package_root)

    settings = settings_module.Settings(_env_file=None)

    assert settings.project_root == runtime_root
    assert settings.data_dir == runtime_root / "data"
    assert settings.soul_path == package_root / "config" / "soul.md"
    assert settings.bundled_skills_dir == package_root / "skills_bundled"


def test_run_cli_parser_accepts_tui_contract_defaults(monkeypatch):
    import run as run_module

    monkeypatch.delenv("NYMERIA_CLI_RICH_SCROLL_REGION", raising=False)

    args = run_module.build_parser().parse_args(["cli"])
    runtime_config = run_module.build_cli_runtime_config(args)

    assert args.thread is None
    assert runtime_config.transport == "api"
    # auto resolves to rich on any interactive terminal (and, via the
    # Windows console probe, on Windows shells that set no TERM); only an
    # explicit `--renderer rich` forces it past the heuristics.
    assert runtime_config.renderer == "auto"
    assert runtime_config.api_url is None
    assert runtime_config.api_key is None
    assert runtime_config.user_id == "default"
    assert runtime_config.user_id_explicit is False
    assert runtime_config.animation is True
    assert runtime_config.ascii_only is False
    assert runtime_config.color == "auto"
    assert runtime_config.rich_scroll_region is True
    assert runtime_config.startup_thread_ref is None
    assert runtime_config.list_threads_on_startup is False


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["cli"], "api"),
        (["cli", "--thin"], "api"),
        (["cli", "--fat"], "local"),
        (["cli", "--transport", "local"], "local"),
        (["cli", "--transport", "auto"], "auto"),
    ],
)
def test_run_cli_parser_transport_aliases(argv, expected):
    import run as run_module

    args = run_module.build_parser().parse_args(argv)
    assert run_module.build_cli_runtime_config(args).transport == expected


@pytest.mark.parametrize(
    "argv",
    [
        ["cli", "--fat", "--thin"],
        ["cli", "--fat", "--transport", "api"],
        ["cli", "--fat", "--transport", "local"],
        ["cli", "--thin", "--transport", "api"],
    ],
)
def test_run_cli_parser_transport_aliases_are_mutually_exclusive(argv):
    import run as run_module

    # --fat/--thin/--transport share a mutually exclusive group, so any two
    # together is a usage error (argparse exits 2) even when they agree.
    with pytest.raises(SystemExit):
        run_module.build_parser().parse_args(argv)


def test_run_cli_parser_keep_db_backend_flag():
    import run as run_module

    assert run_module.build_parser().parse_args(["cli"]).keep_db_backend is False
    parsed = run_module.build_parser().parse_args(["cli", "--fat", "--keep-db-backend"])
    assert parsed.keep_db_backend is True
    assert parsed.transport == "local"


def test_apply_fat_runtime_env_forces_sqlite_and_redis_off(monkeypatch):
    """The fat-mode helper isolates persistence to local SQLite (Redis off) and,
    unlike the slim helper, sets NO API host/port/URL because fat mode binds no
    server. It mutates os.environ directly, so snapshot/restore around it.
    """
    import os

    import run as run_module

    monkeypatch.setenv("DATABASE_BACKEND", "postgres")
    monkeypatch.setenv("REDIS_ENABLED", "true")
    monkeypatch.setenv("REDIS_URL", "redis://stale:6379")
    for key in ("API_HOST", "API_PORT", "NYMERIA_API_URL"):
        monkeypatch.delenv(key, raising=False)

    saved_env = os.environ.copy()
    try:
        run_module._apply_fat_runtime_env()

        assert os.environ["DATABASE_BACKEND"] == "sqlite"
        assert os.environ["REDIS_ENABLED"] == "false"
        assert "REDIS_URL" not in os.environ
        # Fat mode binds no server: the helper must not introduce server env.
        for key in ("API_HOST", "API_PORT", "NYMERIA_API_URL"):
            assert key not in os.environ

        from nymeria.config import get_settings

        settings = get_settings()
        assert settings.database_backend == "sqlite"
        assert settings.redis_enabled is False
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        from nymeria.config import get_settings

        get_settings.cache_clear()


def _run_main_capturing_cli_env(monkeypatch, argv, env_key):
    """Run main() for a cli argv with run_cli/validate_config stubbed, returning
    the value of ``env_key`` in os.environ as seen by run_cli.

    ``_load_environment`` is stubbed with them: since #294 moved it out of
    import scope into ``main()``, it runs AFTER this test's ``monkeypatch``
    calls, and ``override=True`` would let a real ``.env.docker`` beat the very
    pin under test (the shipped ``.env.docker.example`` carries ``API_PORT``).
    That the loader runs, and runs first, is covered by
    ``tests/test_run_command_dispatch.py``. The env snapshot below is now
    belt-and-braces for this test's own tail; conftest layer 3 owns suite-level
    leakage.
    """
    import os
    import sys

    import run as run_module

    captured = {}

    def fake_run_cli(_args):
        captured["value"] = os.environ.get(env_key)

    monkeypatch.setattr(run_module, "run_cli", fake_run_cli)
    monkeypatch.setattr(run_module, "validate_config", lambda *a, **k: None)
    monkeypatch.setattr(run_module, "_load_environment", lambda: None)
    monkeypatch.setattr(sys, "argv", argv)

    saved_env = os.environ.copy()
    try:
        run_module.main()
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        from nymeria.config import get_settings

        get_settings.cache_clear()
    return captured["value"]


def test_fat_main_pins_sqlite_before_run_cli(monkeypatch):
    """main() must pin DATABASE_BACKEND=sqlite for `cli --fat` BEFORE run_cli
    (and before validate_config caches settings), so a Postgres .env.docker
    merged at import cannot make the embedded agent persist to Postgres.
    """
    monkeypatch.setenv("DATABASE_BACKEND", "postgres")
    value = _run_main_capturing_cli_env(
        monkeypatch, ["run.py", "cli", "--fat"], "DATABASE_BACKEND"
    )
    assert value == "sqlite"


def test_fat_keep_db_backend_skips_pin(monkeypatch):
    """--keep-db-backend opts out of the pin: the configured backend is honored."""
    monkeypatch.setenv("DATABASE_BACKEND", "postgres")
    value = _run_main_capturing_cli_env(
        monkeypatch, ["run.py", "cli", "--fat", "--keep-db-backend"], "DATABASE_BACKEND"
    )
    assert value == "postgres"


def test_thin_mode_does_not_pin_db_backend(monkeypatch):
    """The thin (default) CLI never embeds an agent, so it must not pin the
    backend; the env value passes through untouched.
    """
    monkeypatch.setenv("DATABASE_BACKEND", "postgres")
    value = _run_main_capturing_cli_env(
        monkeypatch, ["run.py", "cli"], "DATABASE_BACKEND"
    )
    assert value == "postgres"


def test_resolve_slim_port_precedence(monkeypatch):
    """Explicit --port wins, then the config/env API_PORT, then 8000.

    Both resolution sites (main()'s early env pin and run_slim) must share
    this helper: if only one changed, the early pin would force API_PORT=8000
    into the env before run_slim resolves, silently defeating a configured
    port. Dockerfile.single relies on explicit-flag-wins (it pins --port 8000
    so an API_PORT injected via the compose env_file cannot move the
    container's internal bind).
    """
    import argparse

    import run as run_module

    monkeypatch.setenv("API_PORT", "8010")
    assert run_module._resolve_slim_port(argparse.Namespace(port=None)) == 8010
    assert run_module._resolve_slim_port(argparse.Namespace(port=8123)) == 8123
    monkeypatch.setenv("API_PORT", "junk")
    assert run_module._resolve_slim_port(argparse.Namespace(port=None)) == 8000
    monkeypatch.delenv("API_PORT", raising=False)
    assert run_module._resolve_slim_port(argparse.Namespace(port=None)) == 8000
    args = run_module.build_parser().parse_args(["slim"])
    assert args.port is None  # default defers to API_PORT from the config


def test_slim_main_early_env_pin_resolves_config_port(monkeypatch):
    """main()'s early `_apply_slim_runtime_env` must resolve the port through
    `_resolve_slim_port`, not a hardcoded default: it MUTATES the process env
    before run_slim, so a regression there silently pins API_PORT=8000 for
    every configured port while all the unit tests still pass.
    """
    import os
    import sys

    import run as run_module

    captured = {}

    def fake_run_slim(_args):
        captured["env_port"] = os.environ.get("API_PORT")

    monkeypatch.setenv("API_PORT", "8010")
    monkeypatch.setattr(run_module, "run_slim", fake_run_slim)
    # Stubbed because main() now loads the deployment dotenv with override=True
    # (#294), which would beat the API_PORT pin this test is asserting on.
    monkeypatch.setattr(run_module, "_load_environment", lambda: None)
    monkeypatch.setattr(sys, "argv", ["run.py", "slim"])
    # The early pin rewrites several env vars (DATABASE_BACKEND, REDIS_*,
    # NYMERIA_API_URL); snapshot and restore so nothing leaks into the rest of
    # this test. Suite-level leakage is conftest layer 3's job.
    saved_env = os.environ.copy()
    try:
        run_module.main()
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        from nymeria.config import get_settings

        get_settings.cache_clear()
    assert captured["env_port"] == "8010"


def test_run_cli_parser_accepts_positional_list_command():
    import run as run_module

    args = run_module.build_parser().parse_args(["cli", "list"])
    runtime_config = run_module.build_cli_runtime_config(args)

    assert args.thread is None
    assert runtime_config.startup_thread_ref == "list"
    assert runtime_config.list_threads_on_startup is True


def test_run_cli_parser_thread_flag_can_open_thread_named_list():
    import run as run_module

    args = run_module.build_parser().parse_args(["cli", "--thread", "list"])
    runtime_config = run_module.build_cli_runtime_config(args)

    assert runtime_config.startup_thread_ref == "list"
    assert runtime_config.list_threads_on_startup is False


def test_run_cli_parser_accepts_positional_multi_word_thread_title():
    import run as run_module

    args = run_module.build_parser().parse_args(["cli", "Quarterly", "Planning"])
    runtime_config = run_module.build_cli_runtime_config(args)

    assert args.thread is None
    assert runtime_config.startup_thread_ref == "Quarterly Planning"
    assert runtime_config.list_threads_on_startup is False


@pytest.mark.parametrize(
    ("thread_flag", "thread_id"),
    [
        ("--thread", "alpha"),
        ("--thread-id", "beta"),
        ("-t", "gamma"),
    ],
)
def test_run_cli_parser_thread_aliases(thread_flag: str, thread_id: str):
    import run as run_module

    args = run_module.build_parser().parse_args(["cli", thread_flag, thread_id])
    runtime_config = run_module.build_cli_runtime_config(args)

    assert args.thread == thread_id
    assert runtime_config.startup_thread_ref == thread_id
    assert runtime_config.list_threads_on_startup is False


def test_run_cli_parser_carries_explicit_tui_contract_flags():
    import run as run_module

    args = run_module.build_parser().parse_args(
        [
            "cli",
            "--transport",
            "api",
            "--renderer",
            "plain",
            "--api-url",
            "http://localhost:8000",
            "--api-key",
            "nym_test",
            "--user-id",
            "owner",
            "--no-animation",
            "--ascii",
            "--color",
            "never",
            "--rich-scroll-region",
        ]
    )
    runtime_config = run_module.build_cli_runtime_config(args)

    assert runtime_config.transport == "api"
    assert runtime_config.renderer == "plain"
    assert runtime_config.api_url == "http://localhost:8000"
    assert runtime_config.api_key == "nym_test"
    assert runtime_config.user_id == "owner"
    assert runtime_config.user_id_explicit is True
    assert runtime_config.animation is False
    assert runtime_config.ascii_only is True
    assert runtime_config.color == "never"
    assert runtime_config.rich_scroll_region is True


def test_run_cli_parser_accepts_rich_scroll_region_opt_out(monkeypatch):
    import run as run_module

    monkeypatch.delenv("NYMERIA_CLI_RICH_SCROLL_REGION", raising=False)

    args = run_module.build_parser().parse_args(["cli", "--no-rich-scroll-region"])
    runtime_config = run_module.build_cli_runtime_config(args)

    assert runtime_config.rich_scroll_region is False


def test_run_cli_parser_accepts_rich_scroll_region_env(monkeypatch):
    import run as run_module

    monkeypatch.setenv("NYMERIA_CLI_RICH_SCROLL_REGION", "1")

    args = run_module.build_parser().parse_args(["cli"])
    runtime_config = run_module.build_cli_runtime_config(args)

    assert runtime_config.rich_scroll_region is True


def test_run_cli_parser_accepts_rich_scroll_region_env_opt_out(monkeypatch):
    import run as run_module

    monkeypatch.setenv("NYMERIA_CLI_RICH_SCROLL_REGION", "0")

    args = run_module.build_parser().parse_args(["cli"])
    runtime_config = run_module.build_cli_runtime_config(args)

    assert runtime_config.rich_scroll_region is False


def test_run_cli_parser_accepts_resume_ref():
    import run as run_module

    args = run_module.build_parser().parse_args(["cli", "--resume", "thread-123"])
    runtime_config = run_module.build_cli_runtime_config(args)

    assert runtime_config.resume_ref == "thread-123"


def test_run_cli_default_starts_without_local_agent(monkeypatch):
    import run as run_module
    import nymeria.triggers.cli as cli_module

    captured = {}

    def fake_start_cli(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cli_module, "run_cli", fake_start_cli)

    args = run_module.build_parser().parse_args(["cli"])
    run_module.run_cli(args)

    assert captured["agent"] is None
    assert captured["runtime_config"].transport == "api"
