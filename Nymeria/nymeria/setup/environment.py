"""Environment detection for the first-run wizard.

Pure, dependency-light helpers (no Textual, no settings import) so the detection
is unit-testable and the steps just render the result. Two depths:

- LIGHT (default): no subprocesses at all (PATH lookups, socket probes, file
  markers, /proc/meminfo, disk_usage). Used by the headless path so scripted
  runs stay fast and hermetic.
- DEEP: adds the subprocess probes (docker daemon, compose plugin, running
  containers, port owner), run concurrently with per-probe timeouts. Used by
  the interactive wizard, cached on WizardState before the app starts.

The report drives the welcome screen, the hosting step's gates (see
``hosting_gates``: impossible shapes grey out, degraded ones warn), the review
heads-ups, and the headless gating in the runner. Never raises.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from ..onboarding import DockerStack, HostingOption

# Subprocess probes are best-effort: a hung docker daemon answers `docker info`
# slowly, so every probe carries its own deadline and failure degrades to
# "unknown" (None) rather than an error.
PROBE_TIMEOUT_SECONDS = 5.0
PORT_OWNER_TIMEOUT_SECONDS = 2.0

# Warn-only resource thresholds for the Docker shapes (HYBRID gating: low
# resources never block, they warn). The full stack builds a large Kali-based
# image plus Postgres and Redis; the slim container is a plain Python image.
FULL_STACK_MIN_RAM_GB = 4.0
FULL_STACK_MIN_DISK_GB = 25.0
SLIM_DOCKER_MIN_DISK_GB = 10.0

# The full Docker stack also publishes the MCP server on this port.
MCP_PORT = 8001

# Module-level so tests can point it at a temp file (service_install pattern).
_MEMINFO_PATH = Path("/proc/meminfo")


@dataclass(frozen=True)
class EnvironmentReport:
    """What the wizard could detect about this machine, plus a recommendation."""

    os_label: str
    is_windows: bool
    docker_available: bool
    recommended_hosting: HostingOption
    api_port: int = 8000
    api_port_free: bool = True
    notes: list[str] = field(default_factory=list)
    in_container: bool = False
    # Label of the platform service manager ("systemd user service" / "launchd
    # agent"); a non-empty blocked reason means a service install would
    # certainly fail here, so the hosting step greys that shape out.
    service_manager_label: str = ""
    service_blocked_reason: str = ""
    # None = not probed (light mode or docker absent); True/False = probed.
    docker_daemon_running: bool | None = None
    docker_compose_available: bool | None = None
    nymeria_containers: tuple[str, ...] = ()
    # Best-effort name of the process holding a busy api_port (deep mode;
    # empty when not visible, e.g. another user's process without root).
    port_owner: str = ""
    # First free port above api_port when it is busy (light and deep).
    suggested_port: int | None = None
    total_ram_gb: float | None = None
    free_disk_gb: float | None = None
    mcp_port_free: bool | None = None
    deep: bool = False

    @property
    def port_8000_free(self) -> bool:
        """Back-compat alias from before the port became configurable."""
        return self.api_port_free


@dataclass(frozen=True)
class HostingGate:
    """Detection-driven availability of one hosting shape (HYBRID policy)."""

    # Certainly impossible: rendered greyed-out with the reason as a suffix;
    # the headless path rejects an explicit flag for it.
    disabled: bool = False
    reason: str = ""
    # Degraded but selectable: shown as a warning, never blocks.
    warning: str = ""


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


def suggest_free_port(start: int, *, tries: int = 20) -> int | None:
    """First free port above `start`, or None when the next `tries` are all busy."""
    for candidate in range(start + 1, min(start + 1 + tries, 65536)):
        if port_free(candidate):
            return candidate
    return None


def docker_daemon_running(*, timeout: float = PROBE_TIMEOUT_SECONDS) -> bool | None:
    """Whether `docker info` answers; None when docker is absent or the probe broke."""
    if shutil.which("docker") is None:
        return None
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.returncode == 0


def docker_compose_available(*, timeout: float = PROBE_TIMEOUT_SECONDS) -> bool | None:
    """Whether the `docker compose` plugin answers; None when docker is absent."""
    if shutil.which("docker") is None:
        return None
    try:
        proc = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.returncode == 0


def list_nymeria_containers(*, timeout: float = PROBE_TIMEOUT_SECONDS) -> tuple[str, ...]:
    """Names of running nymeria* containers (an existing install), best effort."""
    if shutil.which("docker") is None:
        return ()
    try:
        proc = subprocess.run(
            ["docker", "ps", "--filter", "name=nymeria", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if proc.returncode != 0:
        return ()
    return tuple(name for name in proc.stdout.split() if name)


def identify_port_owner(
    port: int, *, timeout: float = PORT_OWNER_TIMEOUT_SECONDS
) -> str:
    """Best-effort name of the process listening on the loopback port.

    Tries `ss -tlnp` (Linux), then `lsof` (Linux fallback and the macOS path).
    Returns "" when the owner is not visible: tool missing, probe timed out,
    or the socket belongs to another user and we are not root.
    """
    if sys.platform.startswith("win"):
        return ""
    return _port_owner_from_ss(port, timeout=timeout) or _port_owner_from_lsof(
        port, timeout=timeout
    )


def _port_owner_from_ss(port: int, *, timeout: float) -> str:
    if shutil.which("ss") is None:
        return ""
    try:
        proc = subprocess.run(
            ["ss", "-tlnp"], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4 or not parts[3].endswith(f":{port}"):
            continue
        match = re.search(r'users:\(\("([^"]+)"', line)
        if match:
            return match.group(1)
    return ""


def _port_owner_from_lsof(port: int, *, timeout: float) -> str:
    if shutil.which("lsof") is None:
        return ""
    try:
        proc = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fc"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    for line in proc.stdout.splitlines():
        if line.startswith("c") and len(line) > 1:
            return line[1:]
    return ""


def total_ram_gb() -> float | None:
    """Total system RAM in GB, or None when it cannot be read. Never raises."""
    try:
        meminfo = _MEMINFO_PATH.read_text()
    except OSError:
        meminfo = ""
    for line in meminfo.splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) / (1024 * 1024)
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError):
        return None
    if pages <= 0 or page_size <= 0:
        return None
    return pages * page_size / (1024**3)


def free_disk_gb(path: Path | None = None) -> float | None:
    """Free disk space at `path` (default: cwd) in GB, or None. Never raises."""
    try:
        usage = shutil.disk_usage(path or Path.cwd())
    except OSError:
        return None
    return usage.free / (1024**3)


def _detect_in_container() -> bool:
    """Whether this process runs inside a container (docker/podman markers)."""
    try:
        # Function-local so the markers are read from service_install at call
        # time (tests monkeypatch that module's paths).
        from ..service_install import _in_container

        return _in_container()
    except Exception:
        return False


def service_manager_block(*, platform_name: str | None = None) -> tuple[str, str]:
    """(label, blocked_reason) for the background-service hosting shape.

    A non-empty reason means a service install would certainly fail on this
    host (the cases service_install refuses outright). Degraded-but-possible
    states (e.g. a headless macOS session) stay un-blocked here and keep
    failing softly at install time.
    """
    plat = platform_name or sys.platform
    if plat.startswith("win"):
        return "", "not automated on Windows"
    if plat == "darwin":
        return "launchd agent", ""
    if plat.startswith("linux"):
        from ..service_install import _SYSTEMD_MARKER

        if _detect_in_container():
            return "systemd user service", "running inside a container"
        if not _SYSTEMD_MARKER.exists():
            return "systemd user service", "systemd is not running"
        return "systemd user service", ""
    return "", f"not supported on {plat}"


def recommend_hosting(
    *, is_windows: bool, has_docker: bool, in_container: bool = False
) -> HostingOption:
    """Pick a default hosting shape from what we detected.

    Windows native installs are painful (no project deps, no venv, `sqlite-vec`
    to build), so a container is preferred there when Docker exists. Inside a
    container the foreground process is the only sensible shape. Everywhere
    else a plain local process is the simplest starting point.
    """
    if in_container:
        return HostingOption.LOCAL
    if is_windows and has_docker:
        return HostingOption.DOCKER
    return HostingOption.LOCAL


def hosting_gates(report: EnvironmentReport) -> dict[HostingOption, HostingGate]:
    """Detection-driven gate per hosting option (absent key = fully available).

    LOCAL is never gated: every host can run a foreground process, and the
    picker must keep at least one enabled choice.
    """
    gates: dict[HostingOption, HostingGate] = {}
    if not report.docker_available:
        gates[HostingOption.DOCKER] = HostingGate(
            disabled=True, reason="docker is not installed"
        )
    elif report.docker_daemon_running is False:
        gates[HostingOption.DOCKER] = HostingGate(
            warning="the Docker daemon is not running; start it before launching."
        )
    elif report.docker_compose_available is False:
        gates[HostingOption.DOCKER] = HostingGate(
            warning=(
                "the docker compose plugin was not found; install it before "
                "launching."
            )
        )
    if report.service_blocked_reason:
        gates[HostingOption.SERVICE] = HostingGate(
            disabled=True, reason=report.service_blocked_reason
        )
    return gates


def stack_resource_warnings(
    report: EnvironmentReport, docker_stack: DockerStack | None
) -> list[str]:
    """Warn-only RAM/disk/port notes for the chosen Docker stack. Never blocks."""
    warnings: list[str] = []
    stack = docker_stack or DockerStack.SLIM
    if stack is DockerStack.FULL:
        if (
            report.total_ram_gb is not None
            and report.total_ram_gb < FULL_STACK_MIN_RAM_GB
        ):
            warnings.append(
                f"this machine has {report.total_ram_gb:.1f} GB RAM; the full "
                f"stack (Postgres + Redis + API) wants "
                f"{FULL_STACK_MIN_RAM_GB:.0f} GB or more."
            )
        if (
            report.free_disk_gb is not None
            and report.free_disk_gb < FULL_STACK_MIN_DISK_GB
        ):
            warnings.append(
                f"only {report.free_disk_gb:.0f} GB of disk is free; the full "
                f"image build needs roughly {FULL_STACK_MIN_DISK_GB:.0f} GB."
            )
        if report.mcp_port_free is False:
            warnings.append(
                f"port {MCP_PORT} (the MCP server) is already in use; the full "
                "stack publishes it."
            )
    elif (
        report.free_disk_gb is not None
        and report.free_disk_gb < SLIM_DOCKER_MIN_DISK_GB
    ):
        warnings.append(
            f"only {report.free_disk_gb:.0f} GB of disk is free; the container "
            f"build needs roughly {SLIM_DOCKER_MIN_DISK_GB:.0f} GB."
        )
    return warnings


def detect_environment(*, port: int = 8000, deep: bool = False) -> EnvironmentReport:
    """Best-effort host probe. Never raises.

    The light pass (default) costs no subprocesses; ``deep=True`` adds the
    docker daemon / compose / containers / port-owner probes, run concurrently
    so the wall time is the slowest single probe, not the sum.
    """
    os_label, is_windows = _os_label()
    has_docker = docker_available()
    free = port_free(port)
    in_container = _detect_in_container()
    service_label, service_reason = service_manager_block()
    recommended = recommend_hosting(
        is_windows=is_windows, has_docker=has_docker, in_container=in_container
    )

    daemon: bool | None = None
    compose: bool | None = None
    containers: tuple[str, ...] = ()
    owner = ""
    mcp_free: bool | None = None
    suggested = suggest_free_port(port) if not free else None

    if deep:
        with ThreadPoolExecutor(max_workers=4) as pool:
            daemon_future = pool.submit(docker_daemon_running) if has_docker else None
            compose_future = (
                pool.submit(docker_compose_available) if has_docker else None
            )
            containers_future = (
                pool.submit(list_nymeria_containers) if has_docker else None
            )
            owner_future = pool.submit(identify_port_owner, port) if not free else None
            if daemon_future is not None:
                daemon = daemon_future.result()
            if compose_future is not None:
                compose = compose_future.result()
            if containers_future is not None:
                containers = containers_future.result()
            if owner_future is not None:
                owner = owner_future.result()
        mcp_free = port_free(MCP_PORT)

    notes: list[str] = []
    if is_windows and not has_docker:
        notes.append(
            "Native Windows installs are painful (no venv, sqlite-vec to build). "
            "Install Docker Desktop to use the recommended container shape."
        )
    if not free:
        busy = f"Port {port} is already in use"
        if owner:
            busy += f" (held by {owner})"
        busy += "."
        if suggested is not None:
            busy += f" Port {suggested} looks free."
        notes.append(busy)
    if containers:
        shown = ", ".join(containers[:4])
        notes.append(
            f"Nymeria containers are already running here ({shown}): this "
            "machine hosts an existing install."
        )
    if in_container:
        notes.append(
            "Running inside a container; the background-service shape is "
            "unavailable."
        )

    return EnvironmentReport(
        os_label=os_label,
        is_windows=is_windows,
        docker_available=has_docker,
        recommended_hosting=recommended,
        api_port=port,
        api_port_free=free,
        notes=notes,
        in_container=in_container,
        service_manager_label=service_label,
        service_blocked_reason=service_reason,
        docker_daemon_running=daemon,
        docker_compose_available=compose,
        nymeria_containers=containers,
        port_owner=owner,
        suggested_port=suggested,
        total_ram_gb=total_ram_gb(),
        free_disk_gb=free_disk_gb(),
        mcp_port_free=mcp_free,
        deep=deep,
    )


__all__ = [
    "EnvironmentReport",
    "HostingGate",
    "detect_environment",
    "docker_available",
    "docker_compose_available",
    "docker_daemon_running",
    "free_disk_gb",
    "hosting_gates",
    "identify_port_owner",
    "list_nymeria_containers",
    "port_free",
    "recommend_hosting",
    "service_manager_block",
    "stack_resource_warnings",
    "suggest_free_port",
    "total_ram_gb",
]
