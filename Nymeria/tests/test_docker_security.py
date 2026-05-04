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
