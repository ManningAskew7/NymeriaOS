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
