from pathlib import Path

import yaml

from nymeria.core.event_bus import redact_url_credentials


ROOT = Path(__file__).resolve().parents[1]


def _load_compose() -> dict:
    with (ROOT / "docker-compose.yml").open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_docker_compose_requires_redis_authentication() -> None:
    services = _load_compose()["services"]

    redis = services["redis"]
    assert redis["command"] == [
        "redis-server",
        "--appendonly",
        "yes",
        "--requirepass",
        "${REDIS_PASSWORD:?REDIS_PASSWORD is required}",
    ]
    assert redis["environment"]["REDIS_PASSWORD"] == "${REDIS_PASSWORD:?REDIS_PASSWORD is required}"

    healthcheck = redis["healthcheck"]["test"]
    assert healthcheck[0] == "CMD-SHELL"
    assert "REDISCLI_AUTH=\"$${REDIS_PASSWORD}\"" in healthcheck[1]
    assert "redis-cli ping" in healthcheck[1]
    assert "${REDIS_PASSWORD:?" not in healthcheck[1]

    api_env = services["api"]["environment"]
    assert api_env["REDIS_ENABLED"] == "true"
    assert api_env["REDIS_URL"] == "redis://:${REDIS_PASSWORD:?REDIS_PASSWORD is required}@redis:6379/0"


def test_env_docker_example_defines_redis_password() -> None:
    env_example = (ROOT / ".env.docker.example").read_text(encoding="utf-8")

    assert "REDIS_PASSWORD=your-secure-redis-password-here" in env_example
    assert "openssl rand -hex 32" in env_example


def test_redis_url_redaction_hides_credentials() -> None:
    assert (
        redact_url_credentials("redis://:secret@redis:6379/0")
        == "redis://:***@redis:6379/0"
    )
    assert (
        redact_url_credentials("rediss://user:secret@example.com:6380/1?ssl_cert_reqs=required")
        == "rediss://user:***@example.com:6380/1?ssl_cert_reqs=required"
    )
    assert redact_url_credentials("redis://redis:6379/0") == "redis://redis:6379/0"
