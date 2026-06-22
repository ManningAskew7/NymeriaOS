import json
import os
from datetime import datetime, timedelta, timezone

from nymeria.core import service_health


def test_service_heartbeat_accepts_fresh_ok_status(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NYMERIA_SERVICE_HEALTH_DIR", str(tmp_path))

    service_health.write_service_heartbeat(
        "worker",
        details={"ticker_running": True},
    )

    assert service_health.check_heartbeat("worker", max_age_seconds=30) == []


def test_service_heartbeat_rejects_unhealthy_status(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NYMERIA_SERVICE_HEALTH_DIR", str(tmp_path))

    service_health.write_service_heartbeat("discord-bot", status="unhealthy")

    errors = service_health.check_heartbeat("discord-bot", max_age_seconds=30)

    assert any("status is 'unhealthy'" in error for error in errors)


def test_service_heartbeat_rejects_stale_timestamp(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NYMERIA_SERVICE_HEALTH_DIR", str(tmp_path))
    path = service_health.heartbeat_path("telegram-bot")
    path.parent.mkdir(parents=True, exist_ok=True)
    stale = datetime.now(timezone.utc) - timedelta(minutes=10)
    path.write_text(
        json.dumps(
            {
                "service": "telegram-bot",
                "status": "ok",
                "pid": os.getpid(),
                "timestamp": stale.isoformat(),
            }
        ),
        encoding="utf-8",
    )

    errors = service_health.check_heartbeat("telegram-bot", max_age_seconds=30)

    assert any("heartbeat is stale" in error for error in errors)


class _StubSettings:
    def __init__(self, *, database_backend="sqlite", redis_enabled=False):
        self.database_backend = database_backend
        self.redis_enabled = redis_enabled
        self.postgres_uri = None
        self.redis_url = None


def test_check_service_worker_loads_settings_once(monkeypatch) -> None:
    calls = {"n": 0}

    def _fake_load():
        calls["n"] += 1
        return _StubSettings()

    monkeypatch.setattr(service_health, "_load_settings", _fake_load)
    monkeypatch.setattr(service_health, "check_heartbeat", lambda *a, **k: [])

    errors = service_health.check_service("worker")

    assert errors == []
    # Previously _check_postgres and _check_redis each loaded settings.
    assert calls["n"] == 1


def test_check_service_worker_reports_postgres_misconfig(monkeypatch) -> None:
    monkeypatch.setattr(
        service_health,
        "_load_settings",
        lambda: _StubSettings(database_backend="postgres"),
    )
    monkeypatch.setattr(service_health, "check_heartbeat", lambda *a, **k: [])

    errors = service_health.check_service("worker")

    # Confirms the single shared settings object reaches _check_postgres.
    assert any("POSTGRES_URI is unset" in error for error in errors)
