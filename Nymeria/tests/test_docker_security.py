from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SECURITY_OPT = ["no-new-privileges:true"]
EXPECTED_CAP_DROP = ["ALL"]
# Approved per-service exceptions to the "no cap_add" rule. Each entry is
# justified inline below. New entries must be reviewed because cap_add
# undoes part of the cap_drop ALL hardening baseline.
PERMITTED_CAP_ADD = {
    # caddy: needs NET_BIND_SERVICE to bind 80/443. Cannot be avoided
    # without sysctl host tweaks or losing automatic Let's Encrypt
    # (which requires :80 for ACME HTTP-01).
    "caddy": {"NET_BIND_SERVICE"},
    # searxng: its entrypoint starts as root, chowns /etc/searxng, then drops
    # to an unprivileged uid. CHOWN + SETGID + SETUID are the minimum caps that
    # privilege-drop path needs on top of the cap_drop ALL baseline. Matches the
    # upstream searxng-docker posture.
    "searxng": {"CHOWN", "SETGID", "SETUID"},
}

# Services whose entrypoint cannot run under no-new-privileges. searxng's
# root -> unprivileged drop relies on its granted setuid/setgid caps, which
# no-new-privileges can break; upstream searxng-docker omits it for the same
# reason. cap_drop ALL plus the minimal cap_add above is the compensating
# control. New entries must be reviewed.
NO_NEW_PRIVILEGES_EXEMPT = {"searxng"}


def _load_compose(filename: str) -> dict:
    with (ROOT / filename).open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _assert_service_hardening(services: dict) -> None:
    for name, service in services.items():
        if name in NO_NEW_PRIVILEGES_EXEMPT:
            assert not service.get("security_opt"), (
                f"{name} is exempt from no-new-privileges and must leave "
                "security_opt unset (see NO_NEW_PRIVILEGES_EXEMPT)"
            )
        else:
            assert service.get("security_opt") == EXPECTED_SECURITY_OPT, name
        assert service.get("cap_drop") == EXPECTED_CAP_DROP, name
        cap_add = set(service.get("cap_add") or [])
        permitted = PERMITTED_CAP_ADD.get(name, set())
        assert cap_add <= permitted, (
            f"{name} adds disallowed capabilities {cap_add - permitted}; "
            f"only {permitted or 'none'} are permitted for this service"
        )


def test_main_compose_services_drop_linux_capabilities() -> None:
    services = _load_compose("docker-compose.yml")["services"]

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
    """The watchdog sweep settings ride the api-env anchor (shared with the
    worker, whose ticker hosts the sweep)."""
    services = _load_compose("docker-compose.yml")["services"]
    api_env = services["api"]["environment"]

    assert api_env["WATCHDOG_INTERVAL_MINUTES"] == "${WATCHDOG_INTERVAL_MINUTES:-5}"
    # The env kill switch must pass through so operators can flip it
    # per-deployment (the sweep re-reads it every cycle).
    assert api_env["NYMERIA_WATCHDOG_DISABLED"] == "${NYMERIA_WATCHDOG_DISABLED:-}"


def test_no_standalone_watchdog_service_remains() -> None:
    """Backlog #78 folded the watchdog into the worker's ticker; a watchdog
    service reappearing in compose means the fold regressed."""
    services = _load_compose("docker-compose.yml")["services"]
    assert "watchdog" not in services


def test_nymeria_services_use_expected_runtime_images() -> None:
    services = _load_compose("docker-compose.yml")["services"]

    full_services = {"api", "worker"}
    slim_services = {"discord-bot", "telegram-bot", "mcp"}

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
        "discord-bot",
        "telegram-bot",
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


AGENT_SERVICES = {"api", "worker"}
THIN_CLIENT_SERVICES = {
    "mcp",
    "discord-bot",
    "telegram-bot",
    "slack-bot",
}
INFRA_SERVICES = {"postgres", "redis"}
# Services not subject to the standard hardening contract (operator-installed
# optional add-ons that come from external images with their own deployment
# stories — currently the GPU voice service). The CPU speaches sidecar is NOT
# exempt: it is the recommended voice profile, so it carries real limits.
EXEMPT_SERVICES = {"qwen3-tts"}
SOURCE_BIND_MOUNT_TARGETS = (
    "/app/nymeria",
    "/app/run.py",
)


def test_thin_client_services_use_read_only_rootfs_with_tmpfs() -> None:
    """Thin-client containers (chat bots, mcp) must lock down
    their rootfs. They never spawn the agent or run bash_execute — there
    is no legitimate reason for them to write outside /tmp or the data
    volume, so an attacker who compromises one should not be able to
    persist artifacts in the container layer.
    """
    services = _load_compose("docker-compose.yml")["services"]
    for name in THIN_CLIENT_SERVICES:
        service = services[name]
        assert service.get("read_only") is True, f"{name} must set read_only: true"
        tmpfs = set(service.get("tmpfs") or [])
        assert {"/tmp", "/home/nymeria"} <= tmpfs, (
            f"{name} tmpfs must cover /tmp and /home/nymeria, got {tmpfs}"
        )


def test_agent_services_keep_writable_rootfs_with_tmp_tmpfs() -> None:
    """Agent-bearing containers (api, worker) intentionally keep a writable
    rootfs so the agent can install packages, modify system config, and
    extend its own sandbox at runtime. The container boundary (cap_drop
    ALL minus the install caps below + no-new-privileges + read-only
    bind mounts + network segmentation) is the security boundary.

    See the ``x-agent-fs`` anchor in docker-compose.yml for design notes.
    Do not "fix" the asymmetry by adding read_only: true here — that would
    break runtime ``apt-get`` / ``pip install`` workflows the agent relies
    on for self-extension.
    """
    services = _load_compose("docker-compose.yml")["services"]
    for name in AGENT_SERVICES:
        service = services[name]
        assert service.get("read_only") in (None, False), (
            f"{name} must NOT set read_only: true (agent needs writable rootfs)"
        )
        tmpfs = set(service.get("tmpfs") or [])
        assert "/tmp" in tmpfs, f"{name} should still mount tmpfs /tmp for hygiene"


def test_agent_services_use_non_root_runtime_without_install_caps() -> None:
    """Agent-bearing containers inherit Dockerfile USER nymeria and do not
    add package-install capabilities in the production compose profile.
    Runtime apt/dpkg installs must use a separate explicit maintenance path.
    """
    services = _load_compose("docker-compose.yml")["services"]
    for name in AGENT_SERVICES:
        service = services[name]
        assert service.get("user") not in ("0:0", "0", "root"), (
            f"{name} must not override Dockerfile USER nymeria"
        )
        assert not service.get("cap_add"), f"{name} must not add Linux capabilities"


def test_source_bind_mounts_are_read_only() -> None:
    """Bind mounts of host source (./nymeria, ./run.py) must
    be read-only inside the container. A container compromise should not
    let an attacker overwrite the host's source tree.
    """
    services = _load_compose("docker-compose.yml")["services"]
    for name, service in services.items():
        for entry in service.get("volumes", []) or []:
            if not isinstance(entry, str):
                continue
            for target in SOURCE_BIND_MOUNT_TARGETS:
                if f":{target}:" in entry or entry.endswith(f":{target}"):
                    assert entry.endswith(":ro"), (
                        f"{name}: source bind mount must be read-only: {entry!r}"
                    )


def test_no_service_mounts_full_env_file() -> None:
    services = _load_compose("docker-compose.yml")["services"]
    for name, service in services.items():
        for entry in service.get("volumes", []) or []:
            assert ".env.docker" not in str(entry), (
                f"{name} must not mount the full .env.docker secret file"
            )


def test_thin_clients_receive_minimal_environment() -> None:
    services = _load_compose("docker-compose.yml")["services"]
    forbidden = {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "EMBEDDING_API_KEY",
        "POSTGRES_URI",
        "REDIS_URL",
        "NYMERIA_SECRETS_KEY",
        "FIREBASE_CREDENTIALS_PATH",
        "GOOGLE_OAUTH_CREDENTIALS",
    }
    for name in THIN_CLIENT_SERVICES:
        env = services[name].get("environment") or {}
        assert "NYMERIA_SERVICE_TOKEN" in env, f"{name} needs the API service token"
        leaked = forbidden.intersection(env)
        assert not leaked, f"{name} receives unrelated high-value secrets: {leaked}"


def test_services_have_resource_limits() -> None:
    """Every long-running service must declare mem_limit and pids_limit so
    a runaway process can't exhaust host memory or fork-bomb the kernel.
    Voice services (operator-installed GPU profile) are exempt.
    """
    services = _load_compose("docker-compose.yml")["services"]
    for name, service in services.items():
        if name in EXEMPT_SERVICES:
            continue
        assert service.get("mem_limit"), f"{name} must set mem_limit"
        assert service.get("pids_limit"), f"{name} must set pids_limit"


def test_caddy_is_only_publicly_exposed_service() -> None:
    """Production deployments expose only Caddy on 80/443. Every other
    host-published port must bind to 127.0.0.1 so it is reachable via SSH
    tunnel for local dev but not from the public internet.

    Voice services are optional, but their host ports must still be
    loopback-only by default.
    """
    services = _load_compose("docker-compose.yml")["services"]
    for name, service in services.items():
        for entry in service.get("ports") or []:
            port_str = entry if isinstance(entry, str) else str(entry.get("published", ""))
            if name == "caddy":
                assert port_str in {"80:80", "443:443"}, (
                    f"caddy must only publish 80/443, got {port_str!r}"
                )
            else:
                assert port_str.startswith("127.0.0.1:"), (
                    f"{name} must bind host port to 127.0.0.1, got {port_str!r}"
                )


def test_network_segmentation_isolates_data_plane() -> None:
    """Postgres and Redis are only reachable from services that legitimately
    speak SQL or RESP. A compromised chat-bot container should not be able
    to open a socket to the database directly.
    """
    cfg = _load_compose("docker-compose.yml")
    services = cfg["services"]
    networks = cfg["networks"]

    assert set(networks.keys()) == {"edge", "backend"}, (
        f"compose must define exactly edge and backend networks, got {set(networks.keys())}"
    )

    expected_data_plane = {"postgres", "redis", "api", "worker"}
    for name, service in services.items():
        if name in EXEMPT_SERVICES:
            continue
        nets = set(service.get("networks") or [])
        if name in expected_data_plane:
            assert "backend" in nets, f"{name} must be on the backend network"
        else:
            assert "backend" not in nets, (
                f"{name} must NOT be on the backend network — it has no "
                f"reason to speak to postgres or redis directly"
            )

    # Data-only services should never touch the edge network.
    for name in ("postgres", "redis"):
        nets = set(services[name].get("networks") or [])
        assert "edge" not in nets, (
            f"{name} must not be on edge — only api and worker proxy "
            f"to it from inside backend"
        )


def test_caddy_service_is_present_and_hardened() -> None:
    """Caddy must exist as the public-facing reverse proxy with the same
    hardening baseline as every other thin-client service, plus the one
    permitted exception (NET_BIND_SERVICE for low-port binding).
    """
    services = _load_compose("docker-compose.yml")["services"]
    assert "caddy" in services, "caddy reverse proxy service is required"
    caddy = services["caddy"]
    assert caddy.get("read_only") is True
    assert caddy.get("cap_drop") == ["ALL"]
    assert set(caddy.get("cap_add") or []) == {"NET_BIND_SERVICE"}
    assert caddy.get("security_opt") == ["no-new-privileges:true"]
    # Must mount the Caddyfile read-only.
    assert any(
        isinstance(v, str)
        and v.startswith("./caddy/Caddyfile:/etc/caddy/Caddyfile")
        and v.endswith(":ro")
        for v in caddy.get("volumes") or []
    ), "caddy must mount ./caddy/Caddyfile read-only"


def test_infra_images_are_digest_pinned() -> None:
    """Third-party images we don't build by default must be
    pinned to a sha256 digest, not a floating tag. A tag like
    ``postgres:15-alpine`` can be re-pointed by the image author or a
    compromised registry; a digest is content-addressed and stable.

    Locally-built images (``nymeria-full:local``, ``nymeria-slim:local``)
    are exempt because we build them from our own Dockerfiles. Optional
    GPU voice images are handled by an explicit-image test below.
    """
    services = _load_compose("docker-compose.yml")["services"]
    must_pin = {"postgres", "redis", "caddy"}
    for name in must_pin:
        image = services[name].get("image", "")
        assert "@sha256:" in image, (
            f"{name} image {image!r} must be digest-pinned (@sha256:...)"
        )


def test_no_floating_latest_tag() -> None:
    """No service may pull ``:latest``. Pinning to an explicit version or
    digest is a soft baseline against accidental upgrades.
    """
    services = _load_compose("docker-compose.yml")["services"]
    for name, service in services.items():
        image = service.get("image", "")
        # Locally-built images use the `:local` tag — that's intentional.
        if image.endswith(":local"):
            continue
        assert not image.endswith(":latest"), (
            f"{name} uses :latest tag; pin to a specific version or digest"
        )


def test_voice_images_are_digest_pinned() -> None:
    """qwen3-tts (GPU add-on) keeps the fail-fast placeholder digest; the
    recommended CPU sidecar (speaches) defaults to a real pinned digest so
    the voice profile works out of the box without drifting on a tag.
    """
    services = _load_compose("docker-compose.yml")["services"]
    for name in ("qwen3-tts", "speaches"):
        image = services[name].get("image", "")
        assert "@sha256:" in image, f"{name} must use digest-pinned image syntax"
        assert ":latest" not in image, f"{name} must not default to :latest"


def test_app_dockerfiles_drop_to_non_root_user() -> None:
    """Both app images must end execution as the ``nymeria`` user.

    Running app containers as uid 999 (matching the named-volume ownership
    set by the Dockerfile's chown) is what lets SQLite write its journal
    files under /data. The earlier root + cap_drop ALL combination silently
    broke the scheduled-TODO flow because root without CAP_DAC_OVERRIDE
    could not create files in a dir owned by another uid.
    """
    for dockerfile_name in ("Dockerfile.full", "Dockerfile.slim"):
        content = (ROOT / dockerfile_name).read_text(encoding="utf-8")
        user_directives = [
            line.strip()
            for line in content.splitlines()
            if line.strip().startswith("USER ")
        ]
        assert user_directives, f"{dockerfile_name} has no USER directive"
        assert user_directives[-1] == "USER nymeria", (
            f"{dockerfile_name} must end as USER nymeria, got {user_directives[-1]!r}"
        )


def test_security_sensitive_dependency_floors_or_pins_are_bumped() -> None:
    from packaging.requirements import Requirement
    from packaging.version import Version

    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

    # Minimum security floor each dependency must meet. These were raised in
    # response to known advisories; the assertion is that requirements.txt
    # cannot install a version BELOW the floor. Raising the floor in
    # requirements.txt above these values is the desired direction and must
    # not fail this test, so we compare the parsed lower bound semantically
    # rather than matching exact strings (which drifts every time a floor or
    # pin is bumped).
    minimum_floors = {
        "fastapi": "0.136.1",
        "starlette": "1.0.0",
        "pydantic": "2.13.4",
        "httpx": "0.27.1",
        "requests": "2.32.0",
        "urllib3": "2.2.2",
        "PyYAML": "6.0.2",
        "Jinja2": "3.1.6",
        "Pillow": "10.4.0",
        "cryptography": "42.0.4",
    }

    # Parse "name -> Requirement" from requirements.txt, skipping comments and
    # -r includes.
    parsed: dict[str, Requirement] = {}
    for raw_line in requirements.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        try:
            req = Requirement(line)
        except Exception:
            continue
        parsed[req.name.lower()] = req

    for name, floor in minimum_floors.items():
        req = parsed.get(name.lower())
        assert req is not None, f"{name} missing from requirements.txt"
        lower_bounds = [
            Version(spec.version)
            for spec in req.specifier
            if spec.operator in (">=", "==", "~=")
        ]
        assert lower_bounds, f"{name} has no lower-bound or pin in {req.specifier!r}"
        effective_floor = max(lower_bounds)
        assert effective_floor >= Version(floor), (
            f"{name} floor {effective_floor} is below the required security "
            f"floor {floor}"
        )
