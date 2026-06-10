"""Generate and start a self-contained CLIProxy deployment.

Used by the wizard's CLIProxy endpoint step when no proxy is reachable: writes
a minimal `cliproxy/` directory (digest-pinned compose + config.yaml + the
plaintext management secret at 0600) under the install root and brings it up
with `docker compose up -d`. The proxy is an external prod dependency pulled
from Docker Hub; nothing here builds from or depends on a vendored source
tree.
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Digest-pinned: floating tags can be reassigned upstream and the cloak gate
# changed once already (v6.9.0 -> v6.9.36). v7.1.61 is the verified baseline
# (cloak smoke test + management OAuth endpoints; see docs/private/cliproxy.md).
CLIPROXY_PINNED_IMAGE = (
    "eceasy/cli-proxy-api:v7.1.61@sha256:"
    "bee212f92a6860d58bcbd587047e1b33713282dd0dbb79c43c299651084714f1"
)

# Host port for a generated deployment (container always listens on 8317).
CLIPROXY_HOST_PORT = 8318

MANAGEMENT_SECRET_FILENAME = "MANAGEMENT_SECRET.txt"


def mint_management_secret() -> str:
    return "cpm-nymeria-" + secrets.token_urlsafe(24)


def mint_gatekeeper_key() -> str:
    return "cpx-nymeria-" + secrets.token_urlsafe(24)


@dataclass(frozen=True)
class CLIProxyDeployment:
    directory: Path
    management_url: str
    management_secret: str
    gatekeeper_key: str


def generate_cliproxy_deployment(
    directory: Path,
    *,
    management_secret: str,
    gatekeeper_key: str,
    join_network: str | None = None,
) -> CLIProxyDeployment:
    """Write the deployment files (idempotent; existing auths/ are kept).

    `join_network` attaches the container to an external Docker network with
    the `cli-proxy-api` alias so a full-stack backend can reach it by name;
    pass the stack's edge network (e.g. `nymeria_edge`) when relevant.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "auths").mkdir(exist_ok=True)
    (directory / "logs").mkdir(exist_ok=True)

    config_lines = [
        'host: "0.0.0.0"',
        "port: 8317",
        "",
        'auth-dir: "/root/.cli-proxy-api"',
        "",
        "# Data-plane gatekeeper keys: what Nymeria sends as its LLM API key.",
        "api-keys:",
        f'  - "{gatekeeper_key}"',
        "",
        "request-retry: 1",
        "",
        "# Management API (/v0/management). The proxy bcrypt-hashes secret-key",
        f"# on first start; the plaintext lives in {MANAGEMENT_SECRET_FILENAME} (0600).",
        "remote-management:",
        f'  secret-key: "{management_secret}"',
        "  allow-remote: true",
        "  disable-control-panel: false",
        "",
    ]
    (directory / "config.yaml").write_text("\n".join(config_lines), encoding="utf-8")

    secret_path = directory / MANAGEMENT_SECRET_FILENAME
    secret_path.write_text(
        "# CLIProxy remote-management secret (plaintext; config.yaml holds a "
        "bcrypt hash after first start).\n" + management_secret + "\n",
        encoding="utf-8",
    )
    secret_path.chmod(0o600)

    network_lines: list[str] = []
    if join_network:
        network_lines = [
            "    networks:",
            "      default: {}",
            f"      {join_network}:",
            "        aliases:",
            "          - cli-proxy-api",
            "",
            "networks:",
            f"  {join_network}:",
            "    external: true",
        ]
    compose_lines = [
        "services:",
        "  cli-proxy-api:",
        "    # PINNED to a digest; bumping it requires re-running",
        "    # Nymeria/tools/check_cliproxy_cloak.py (see docs/private/cliproxy.md).",
        f"    image: {CLIPROXY_PINNED_IMAGE}",
        "    pull_policy: missing",
        "    container_name: nymeria-cliproxy",
        "    ports:",
        f'      - "{CLIPROXY_HOST_PORT}:8317"',
        "    volumes:",
        "      - ./config.yaml:/CLIProxyAPI/config.yaml",
        "      - ./auths:/root/.cli-proxy-api",
        "      - ./logs:/CLIProxyAPI/logs",
        "    restart: unless-stopped",
        *network_lines,
        "",
    ]
    (directory / "docker-compose.yml").write_text(
        "\n".join(compose_lines), encoding="utf-8"
    )

    return CLIProxyDeployment(
        directory=directory,
        management_url=f"http://localhost:{CLIPROXY_HOST_PORT}",
        management_secret=management_secret,
        gatekeeper_key=gatekeeper_key,
    )


def docker_available() -> bool:
    return shutil.which("docker") is not None


def compose_up(directory: Path, *, timeout: float = 300.0) -> tuple[bool, str]:
    """`docker compose up -d` the generated deployment; (ok, detail)."""
    try:
        result = subprocess.run(
            ["docker", "compose", "up", "-d"],
            cwd=str(directory),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, "docker is not installed or not on PATH"
    except subprocess.TimeoutExpired:
        return False, f"docker compose up timed out after {int(timeout)}s"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return False, detail[-400:] or "docker compose up failed"
    return True, ""


__all__ = [
    "CLIPROXY_HOST_PORT",
    "CLIPROXY_PINNED_IMAGE",
    "CLIProxyDeployment",
    "MANAGEMENT_SECRET_FILENAME",
    "compose_up",
    "docker_available",
    "generate_cliproxy_deployment",
    "mint_gatekeeper_key",
    "mint_management_secret",
]
