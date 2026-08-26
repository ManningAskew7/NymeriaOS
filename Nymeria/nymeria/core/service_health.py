"""Runtime health signals for non-API Docker services.

The API has an HTTP ``/health`` route. Worker, bot, and MCP containers do
not all expose HTTP health endpoints, so they publish a small heartbeat
file that Docker can validate from a separate process.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .storage_paths import write_text_atomic

DEFAULT_HEALTH_DIR = "/tmp/nymeria-health"
DEFAULT_MAX_AGE_SECONDS = 90
HEARTBEAT_INTERVAL_SECONDS = 15

HEARTBEAT_SERVICES = {
    "worker",
    "discord-bot",
    "slack-bot",
    "telegram-bot",
    "twitch-bot",
}

SERVICE_CHOICES = sorted({*HEARTBEAT_SERVICES, "mcp"})


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _health_dir() -> Path:
    return Path(os.environ.get("NYMERIA_SERVICE_HEALTH_DIR", DEFAULT_HEALTH_DIR))


def heartbeat_path(service: str) -> Path:
    """Return the heartbeat file path for a service name."""
    safe_name = service.replace("/", "-").replace("..", "-")
    return _health_dir() / f"{safe_name}.json"


def write_service_heartbeat(
    service: str,
    *,
    status: str = "ok",
    details: Mapping[str, Any] | None = None,
) -> None:
    """Atomically write one service heartbeat.

    ``status`` must be ``"ok"`` for Docker health checks to pass. Callers can
    write ``"unhealthy"`` when the process is alive but its client/ticker loop
    is not functional.
    """
    path = heartbeat_path(service)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "service": service,
        "status": status,
        "pid": os.getpid(),
        "timestamp": _now_utc().isoformat(),
    }
    if details:
        payload["details"] = dict(details)

    write_text_atomic(path, json.dumps(payload, sort_keys=True) + "\n")


def _read_heartbeat(service: str) -> tuple[dict[str, Any] | None, list[str]]:
    path = heartbeat_path(service)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, [f"{service}: heartbeat file missing at {path}"]
    except OSError as exc:
        return None, [f"{service}: could not read heartbeat at {path}: {exc}"]

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, [f"{service}: heartbeat is not valid JSON: {exc}"]
    if not isinstance(payload, dict):
        return None, [f"{service}: heartbeat JSON must be an object"]
    return payload, []


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def check_heartbeat(service: str, *, max_age_seconds: int) -> list[str]:
    """Validate a service heartbeat file."""
    payload, errors = _read_heartbeat(service)
    if errors:
        return errors
    assert payload is not None

    if payload.get("service") != service:
        errors.append(f"{service}: heartbeat belongs to {payload.get('service')!r}")

    status = payload.get("status")
    if status != "ok":
        errors.append(f"{service}: heartbeat status is {status!r}")

    timestamp = _parse_timestamp(payload.get("timestamp"))
    if timestamp is None:
        errors.append(f"{service}: heartbeat timestamp is missing or invalid")
    else:
        age = (_now_utc() - timestamp).total_seconds()
        if age > max_age_seconds:
            errors.append(
                f"{service}: heartbeat is stale ({age:.1f}s > {max_age_seconds}s)"
            )
        if age < -5:
            errors.append(f"{service}: heartbeat timestamp is in the future")

    if not _pid_alive(payload.get("pid")):
        errors.append(f"{service}: heartbeat PID is not alive")

    return errors


def _check_api(api_url: str, errors: list[str]) -> None:
    url = f"{api_url.rstrip('/')}/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            body = response.read()
            if response.status != 200:
                errors.append(f"api: /health returned HTTP {response.status}")
                return
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        errors.append(f"api: /health check failed at {url}: {exc}")
        return

    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"api: /health returned invalid JSON: {exc}")
        return
    if data.get("status") != "ok":
        errors.append(f"api: /health status is {data.get('status')!r}")


def _check_tcp(host: str, port: int, label: str, errors: list[str]) -> None:
    try:
        with socket.create_connection((host, port), timeout=5):
            return
    except OSError as exc:
        errors.append(f"{label}: TCP check failed at {host}:{port}: {exc}")


def _load_settings():
    from nymeria.config import get_settings

    return get_settings()


def _check_postgres(errors: list[str], settings) -> None:
    if settings.database_backend != "postgres":
        return
    if not settings.postgres_uri:
        errors.append("postgres: DATABASE_BACKEND=postgres but POSTGRES_URI is unset")
        return
    try:
        import psycopg  # type: ignore[import-untyped]

        with psycopg.connect(settings.postgres_uri, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
    except Exception as exc:  # noqa: BLE001 - health check must report all failures.
        errors.append(f"postgres: SELECT 1 failed: {exc}")


def _check_redis(errors: list[str], settings) -> None:
    if not settings.redis_enabled:
        return
    if not settings.redis_url:
        errors.append("redis: REDIS_ENABLED=true but REDIS_URL is unset")
        return
    try:
        import redis

        client = redis.from_url(
            settings.redis_url,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        try:
            client.ping()
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001 - health check must report all failures.
        errors.append(f"redis: ping failed: {exc}")


def check_service(
    service: str,
    *,
    api_url: str | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    mcp_host: str = "127.0.0.1",
    mcp_port: int = 8001,
) -> list[str]:
    """Run service-specific health checks and return error messages."""
    errors: list[str] = []

    if service in HEARTBEAT_SERVICES:
        errors.extend(check_heartbeat(service, max_age_seconds=max_age_seconds))

    if service == "worker":
        settings = _load_settings()
        _check_postgres(errors, settings)
        _check_redis(errors, settings)

    if service in {"discord-bot", "slack-bot", "telegram-bot", "twitch-bot", "mcp"}:
        _check_api(api_url or "http://nymeria-api:8000", errors)

    if service == "mcp":
        _check_tcp(mcp_host, mcp_port, "mcp", errors)

    if service not in SERVICE_CHOICES:
        errors.append(f"unknown service: {service}")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nymeria service health checks")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser("check", help="Check one service")
    check_parser.add_argument("service", choices=SERVICE_CHOICES)
    check_parser.add_argument("--api-url", default=None)
    check_parser.add_argument(
        "--max-age-seconds",
        type=int,
        default=DEFAULT_MAX_AGE_SECONDS,
    )
    check_parser.add_argument("--mcp-host", default="127.0.0.1")
    check_parser.add_argument("--mcp-port", type=int, default=8001)

    args = parser.parse_args(argv)

    if args.command == "check":
        errors = check_service(
            args.service,
            api_url=args.api_url,
            max_age_seconds=args.max_age_seconds,
            mcp_host=args.mcp_host,
            mcp_port=args.mcp_port,
        )
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        print(f"{args.service}: ok")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
