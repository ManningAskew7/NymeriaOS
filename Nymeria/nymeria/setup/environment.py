"""Environment detection for the first-run wizard's welcome step.

Pure, dependency-light helpers (no Textual, no settings import) so the detection
is unit-testable and the welcome screen just renders the result. This is the
"detect environment" step from the setup-wizard plan: read the host, then
recommend a hosting shape the operator can accept or override.
"""

from __future__ import annotations

import platform
import shutil
import socket
from dataclasses import dataclass, field

from ..onboarding import HostingOption


@dataclass(frozen=True)
class EnvironmentReport:
    """What the wizard could detect about this machine, plus a recommendation."""

    os_label: str
    is_windows: bool
    docker_available: bool
    port_8000_free: bool
    recommended_hosting: HostingOption
    notes: list[str] = field(default_factory=list)


def _os_label() -> tuple[str, bool]:
    system = platform.system()
    mapping = {"Darwin": "macOS", "Windows": "Windows", "Linux": "Linux"}
    label = mapping.get(system, system or "this OS")
    release = platform.release()
    if release:
        label = f"{label} {release}"
    return label, system == "Windows"


def docker_available() -> bool:
    """True when a `docker` CLI is on PATH. Cheap (no `docker info` subprocess)."""
    return shutil.which("docker") is not None


def port_free(port: int, *, host: str = "127.0.0.1", timeout: float = 0.2) -> bool:
    """True when nothing is already listening on the loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) != 0


def recommend_hosting(*, is_windows: bool, has_docker: bool) -> HostingOption:
    """Pick a default hosting shape from what we detected.

    Windows native installs are painful (no project deps, no venv, `sqlite-vec`
    to build), so a container is preferred there when Docker exists. Everywhere
    else a plain local process is the simplest starting point.
    """
    if is_windows and has_docker:
        return HostingOption.DOCKER
    return HostingOption.LOCAL


def detect_environment(*, port: int = 8000) -> EnvironmentReport:
    """Best-effort host probe for the welcome screen. Never raises."""
    os_label, is_windows = _os_label()
    has_docker = docker_available()
    free = port_free(port)
    recommended = recommend_hosting(is_windows=is_windows, has_docker=has_docker)

    notes: list[str] = []
    if is_windows and not has_docker:
        notes.append(
            "Native Windows installs are painful (no venv, sqlite-vec to build). "
            "Install Docker Desktop to use the recommended container shape."
        )
    if not free:
        notes.append(
            f"Port {port} is already in use. Free it or start with a different "
            "port later."
        )
    return EnvironmentReport(
        os_label=os_label,
        is_windows=is_windows,
        docker_available=has_docker,
        port_8000_free=free,
        recommended_hosting=recommended,
        notes=notes,
    )


__all__ = [
    "EnvironmentReport",
    "detect_environment",
    "docker_available",
    "port_free",
    "recommend_hosting",
]
