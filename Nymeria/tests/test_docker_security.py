from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SECURITY_OPT = ["no-new-privileges:true"]
EXPECTED_CAP_DROP = ["ALL"]


def _load_compose(filename: str) -> dict:
    with (ROOT / filename).open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _assert_service_hardening(services: dict) -> None:
    for name, service in services.items():
        assert service.get("security_opt") == EXPECTED_SECURITY_OPT, name
        assert service.get("cap_drop") == EXPECTED_CAP_DROP, name
        assert not service.get("cap_add"), name


def test_main_compose_services_drop_linux_capabilities() -> None:
    services = _load_compose("docker-compose.yml")["services"]

    _assert_service_hardening(services)


def test_hexstrike_compose_services_drop_linux_capabilities() -> None:
    services = _load_compose("docker-compose.hexstrike.yml")["services"]

    _assert_service_hardening(services)


def test_postgres_and_redis_use_read_only_rootfs_with_persistent_data_volumes() -> None:
    services = _load_compose("docker-compose.yml")["services"]

    postgres = services["postgres"]
    assert postgres.get("user") == "postgres"
    assert postgres.get("read_only") is True
    assert set(postgres.get("tmpfs", [])) >= {"/tmp", "/var/run/postgresql"}
    assert "postgres_data:/var/lib/postgresql/data" in postgres["volumes"]

    redis = services["redis"]
    assert redis.get("user") == "redis"
    assert redis.get("read_only") is True
    assert set(redis.get("tmpfs", [])) >= {"/tmp"}
    assert "redis_data:/data" in redis["volumes"]


def test_compose_timezone_defaults_are_consistent() -> None:
    services = _load_compose("docker-compose.yml")["services"]
    api_env = services["api"]["environment"]

    assert api_env["TZ"] == "${USER_TIMEZONE:-UTC}"
    assert api_env["USER_TIMEZONE"] == "${USER_TIMEZONE:-UTC}"


def test_compose_watchdog_default_matches_documented_default() -> None:
    services = _load_compose("docker-compose.yml")["services"]
    api_env = services["api"]["environment"]

    assert api_env["WATCHDOG_INTERVAL_MINUTES"] == "${WATCHDOG_INTERVAL_MINUTES:-5}"


def test_nymeria_services_use_expected_runtime_images() -> None:
    services = _load_compose("docker-compose.yml")["services"]

    full_services = {"api", "worker", "twitch-bot"}
    slim_services = {"watchdog", "discord-bot", "telegram-bot", "mcp"}

    for service_name in full_services:
        service = services[service_name]
        assert service["image"] == "nymeria-full:local"
        assert service["build"]["dockerfile"] == "Dockerfile.full"

    for service_name in slim_services:
        service = services[service_name]
        assert service["image"] == "nymeria-slim:local"
        assert service["build"]["dockerfile"] == "Dockerfile.slim"


def test_slim_dockerfile_omits_agent_workstation_dependencies() -> None:
    dockerfile = (ROOT / "Dockerfile.slim").read_text(encoding="utf-8")

    assert "kalilinux/kali-rolling" not in dockerfile
    assert "playwright install" not in dockerfile
    assert "npm install" not in dockerfile
    assert "requirements-dev.txt" not in dockerfile
    assert "pytest" not in dockerfile


def test_non_api_services_use_runtime_health_checks() -> None:
    services = _load_compose("docker-compose.yml")["services"]

    for service_name in (
        "worker",
        "watchdog",
        "discord-bot",
        "telegram-bot",
        "twitch-bot",
        "mcp",
    ):
        healthcheck = services[service_name]["healthcheck"]["test"]
        command = " ".join(healthcheck)

        assert "pgrep" not in command
        assert "nymeria.core.service_health" in command


def test_shared_dockerfile_has_no_built_in_healthcheck() -> None:
    full_dockerfile = (ROOT / "Dockerfile.full").read_text(encoding="utf-8")
    slim_dockerfile = (ROOT / "Dockerfile.slim").read_text(encoding="utf-8")

    assert "HEALTHCHECK" not in full_dockerfile
    assert "HEALTHCHECK" not in slim_dockerfile
