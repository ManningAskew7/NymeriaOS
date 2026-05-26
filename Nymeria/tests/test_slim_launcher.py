"""Tests for the slim runtime-env helper used by ``python run.py slim``."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


@pytest.fixture()
def slim_run(monkeypatch: pytest.MonkeyPatch):
    """Provide a fresh ``run`` module with the settings cache cleared."""
    # Force a clean settings instance for each test.
    if "run" in sys.modules:
        del sys.modules["run"]
    if "nymeria.config" in sys.modules:
        # Drop the cached settings so env overrides take effect.
        from nymeria.config import get_settings as _get_settings

        _get_settings.cache_clear()
    module = importlib.import_module("run")
    yield module
    from nymeria.config import get_settings as _get_settings

    _get_settings.cache_clear()


def test_apply_slim_runtime_env_forces_sqlite_and_redis_off(
    slim_run, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABASE_BACKEND", "postgres")
    monkeypatch.setenv("REDIS_ENABLED", "true")
    monkeypatch.setenv("REDIS_URL", "redis://stale:6379")
    monkeypatch.delenv("SCHEDULER_MISSED_WORK_POLICY", raising=False)
    monkeypatch.delenv("SCHEDULER_ACTIVE_EXECUTION_STALE_MINUTES", raising=False)

    base_url = slim_run._apply_slim_runtime_env(
        host="127.0.0.1",
        port=8000,
        data_dir=str(tmp_path),
        missed_work_policy="ask",
        active_execution_stale_minutes=10,
    )

    import os

    assert os.environ["DATABASE_BACKEND"] == "sqlite"
    assert os.environ["REDIS_ENABLED"] == "false"
    assert "REDIS_URL" not in os.environ
    assert os.environ["API_HOST"] == "127.0.0.1"
    assert os.environ["API_PORT"] == "8000"
    assert base_url == "http://127.0.0.1:8000"
    assert os.environ["NYMERIA_API_URL"] == "http://127.0.0.1:8000"
    assert os.environ["NYMERIA_DATA_DIR"] == str(tmp_path)
    assert os.environ["SCHEDULER_MISSED_WORK_POLICY"] == "ask"
    assert os.environ["SCHEDULER_ACTIVE_EXECUTION_STALE_MINUTES"] == "10"

    from nymeria.config import get_settings

    settings = get_settings()
    assert settings.database_backend == "sqlite"
    assert settings.redis_enabled is False
    assert settings.api_port == 8000
    assert settings.scheduler_missed_work_policy == "ask"
    assert settings.scheduler_active_execution_stale_minutes == 10


def test_apply_slim_runtime_env_custom_port_updates_url(
    slim_run, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NYMERIA_DATA_DIR", str(tmp_path))

    base_url = slim_run._apply_slim_runtime_env(host="127.0.0.1", port=9123)

    import os

    assert os.environ["API_PORT"] == "9123"
    assert base_url == "http://127.0.0.1:9123"
    assert os.environ["NYMERIA_API_URL"] == "http://127.0.0.1:9123"


def test_apply_slim_runtime_env_rewrites_zero_host_to_loopback(
    slim_run, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NYMERIA_DATA_DIR", str(tmp_path))

    base_url = slim_run._apply_slim_runtime_env(host="0.0.0.0", port=8000)

    import os

    # Bind host should be preserved exactly so uvicorn still listens on 0.0.0.0.
    assert os.environ["API_HOST"] == "0.0.0.0"
    # But loopback clients use 127.0.0.1 because 0.0.0.0 is not connectable.
    assert base_url == "http://127.0.0.1:8000"
    assert os.environ["NYMERIA_API_URL"] == "http://127.0.0.1:8000"


def test_slim_parser_accepts_flags(slim_run) -> None:
    parser = slim_run.build_parser()
    args = parser.parse_args(
        [
            "slim",
            "--host",
            "192.168.1.10",
            "--port",
            "7777",
            "--no-mcp",
            "--no-watchdog",
            "--missed-work-policy",
            "ask",
            "--active-execution-stale-minutes",
            "10",
        ]
    )
    assert args.command == "slim"
    assert args.host == "192.168.1.10"
    assert args.port == 7777
    assert args.no_mcp is True
    assert args.no_watchdog is True
    assert args.missed_work_policy == "ask"
    assert args.active_execution_stale_minutes == 10
