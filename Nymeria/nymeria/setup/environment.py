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

import importlib.util
import math
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

# Timezone detection sources (module constants so tests can point them at
# fixture files; both are absent on Windows, which degrades to no detection).
_ETC_TIMEZONE = Path("/etc/timezone")
_ETC_LOCALTIME = Path("/etc/localtime")


def detect_system_timezone() -> str | None:
    """Best-effort IANA timezone detection for this host.

    Sources, in order: the ``TZ`` env var (leading ``:`` stripped),
    ``/etc/timezone`` (Debian family), then the ``/etc/localtime`` symlink
    target (most other Linux and macOS). Every candidate is validated against
    the zoneinfo database; returns None when nothing valid is found (e.g.
    Windows, or a container without tz data), in which case the caller falls
    back to the UTC default. No subprocesses, never raises.
    """
    candidates: list[str] = []
    tz_env = os.environ.get("TZ", "").lstrip(":").strip()
    if tz_env:
        candidates.append(tz_env)
    try:
        candidates.append(_ETC_TIMEZONE.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        pass  # absent on non-Debian/Windows, or unreadable: try the next source
    try:
        target = os.path.realpath(_ETC_LOCALTIME)
        if "zoneinfo/" in target:
            name = target.split("zoneinfo/", 1)[1]
            # Some distros symlink through the posix/ or right/ trees; the
            # plain name is the canonical IANA spelling.
            for prefix in ("posix/", "right/"):
                if name.startswith(prefix):
                    name = name[len(prefix):]
            candidates.append(name)
    except OSError:
        pass  # no /etc/localtime (e.g. Windows): fall through to UTC
    for name in candidates:
        if not name:
            continue
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(name)
        except Exception:  # noqa: BLE001 (ZoneInfoNotFoundError, ValueError, ...)
            continue
        return name
    return None

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

# GUI browsers (plus the xdg-open/sensible-browser dispatchers) that make a
# DISPLAY-bearing Linux session able to land an OAuth page in front of the
# user. Windows and macOS are assumed to have a default browser.
_BROWSER_BINARIES = (
    "xdg-open",
    "sensible-browser",
    "firefox",
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "brave-browser",
)

# Marker set for the native-deps check: heavyweight runtime requirements that
# any pip install carries (hard requirements). Honest limitation: the wizard's
# own import chain currently pulls in langgraph and langchain_core, so those
# two can only report missing if the wizard's imports slim down later; the
# live signal today is fastapi/uvicorn absent (a partial install). The full
# set stays as cheap insurance for both futures.
_RUNTIME_DEP_MODULES = ("fastapi", "uvicorn", "langgraph", "langchain_core")


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
    # Why webbrowser.open() would not land in front of this user ("" = it
    # would): SSH session, container, display-less host. Light signal.
    browser_blocked_reason: str = ""
    # Backend runtime packages this interpreter cannot import (light signal;
    # empty for any pip install, non-empty in an uninstalled source checkout).
    missing_python_deps: tuple[str, ...] = ()
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
    """First free port above `start`, or None when the next `tries` are all busy.

    Never suggests the MCP port: the full stack publishes it even when nothing
    is listening there yet, so it only looks free.
    """
    for candidate in range(start + 1, min(start + 1 + tries, 65536)):
        if candidate == MCP_PORT:
            continue
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


def browser_launch_blocked_reason(*, platform_name: str | None = None) -> str:
    """Why opening a URL would not land in front of this user ("" = it would).

    Light heuristics only (env vars, file markers, PATH lookups, no
    subprocess): an SSH session would open the browser on the wrong machine
    (hermes-agent's signal set), a container or a display-less Linux host has
    nowhere to open one. WSL interop opens the Windows-side browser even
    without a display. Windows and macOS are assumed to have a usable default
    browser.
    """
    if any(os.environ.get(var) for var in ("SSH_CLIENT", "SSH_TTY", "SSH_CONNECTION")):
        return "SSH session"
    plat = platform_name or sys.platform
    if plat.startswith("win") or plat == "darwin":
        return ""
    if _detect_in_container():
        return "running inside a container"
    if _detect_wsl():
        return ""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return "no graphical session"
    if os.environ.get("BROWSER"):
        # The user explicitly configured a launcher; webbrowser honors it.
        return ""
    if not any(shutil.which(binary) for binary in _BROWSER_BINARIES):
        return "no web browser found"
    return ""


def missing_python_deps(
    modules: tuple[str, ...] = _RUNTIME_DEP_MODULES,
) -> tuple[str, ...]:
    """Backend runtime packages this interpreter cannot import.

    ``find_spec`` locates without importing, so the check is cheap. The same
    interpreter that runs the wizard runs the backend in every native shape
    (local foreground, background service), so a miss here means those shapes
    fail at launch. Best effort: a broken finder counts as present so the
    wizard never warns on its own machinery.
    """
    missing: list[str] = []
    for module in modules:
        try:
            spec = importlib.util.find_spec(module)
        except Exception:  # noqa: BLE001 (best effort; never block the wizard)
            continue
        if spec is None:
            missing.append(module)
    return tuple(missing)


def _detect_in_container() -> bool:
    """Whether this process runs inside a container (docker/podman markers)."""
    try:
        # Function-local so the markers are read from service_install at call
        # time (tests monkeypatch that module's paths).
        from ..service_install import _in_container

        return _in_container()
    except Exception:
        return False


def _detect_wsl() -> bool:
    """Whether this is WSL (interop opens URLs in the Windows-side browser).

    Uses the WSL_DISTRO_NAME / /proc/version markers via service_install
    (function-local import for the same monkeypatch-at-call-time reason);
    a wslview-on-PATH check would misfire both ways (wslu installs fine on
    plain servers, and real WSL works without it via explorer.exe).
    """
    try:
        from ..service_install import _is_wsl

        return _is_wsl()
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
    *,
    is_windows: bool,
    has_docker: bool,
    in_container: bool = False,
    native_deps_missing: bool = False,
    service_blocked: bool = True,
) -> HostingOption:
    """Pick a default hosting shape from what we detected.

    Windows native installs are painful (no project deps, no venv, `sqlite-vec`
    to build), so a container is preferred there when Docker exists; the same
    holds anywhere the runtime's Python packages are missing (the container
    image carries them). Inside a container the foreground process is the only
    sensible shape. On Linux/macOS with a working service manager and the
    runtime deps present, the background service is the better default: it
    survives the terminal closing and starts on login, which is what a
    non-technical install actually wants. ``service_blocked`` defaults to True
    (= keep recommending LOCAL) so direct callers without detection data get
    the conservative pre-2026-07 behavior; ``detect_environment`` passes the
    real ``service_manager_block`` verdict.
    """
    if in_container:
        return HostingOption.LOCAL
    if has_docker and (is_windows or native_deps_missing):
        return HostingOption.DOCKER
    if not service_blocked and not native_deps_missing and not is_windows:
        return HostingOption.SERVICE
    return HostingOption.LOCAL


def hosting_gates(report: EnvironmentReport) -> dict[HostingOption, HostingGate]:
    """Detection-driven gate per hosting option (absent key = fully available).

    LOCAL is never disabled: every host can run a foreground process, and the
    picker must keep at least one enabled choice (it can still carry a
    warning, e.g. missing runtime packages).
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
    if report.missing_python_deps:
        # Both native shapes run this interpreter; warn, never block (a pip
        # install between now and launch fixes it).
        deps_warning = (
            "the backend's Python packages are not importable here (missing: "
            f"{', '.join(report.missing_python_deps)}); install the project "
            "dependencies before launching."
        )
        gates[HostingOption.LOCAL] = HostingGate(warning=deps_warning)
        if HostingOption.SERVICE not in gates:
            gates[HostingOption.SERVICE] = HostingGate(warning=deps_warning)
    return gates


def _floor1(value: float) -> float:
    """Round down to one decimal: a sub-threshold value must never display as
    the threshold itself (9.96 GB free reading as "10 GB free; needs 10 GB")."""
    return math.floor(value * 10) / 10


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
                f"this machine has {_floor1(report.total_ram_gb):.1f} GB RAM; "
                f"the full stack (Postgres + Redis + API) wants "
                f"{FULL_STACK_MIN_RAM_GB:.0f} GB or more."
            )
        if (
            report.free_disk_gb is not None
            and report.free_disk_gb < FULL_STACK_MIN_DISK_GB
        ):
            warnings.append(
                f"only {_floor1(report.free_disk_gb):.1f} GB of disk is free; "
                f"the full image build needs roughly "
                f"{FULL_STACK_MIN_DISK_GB:.0f} GB."
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
            f"only {_floor1(report.free_disk_gb):.1f} GB of disk is free; the "
            f"container build needs roughly {SLIM_DOCKER_MIN_DISK_GB:.0f} GB."
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
    browser_reason = browser_launch_blocked_reason()
    missing_deps = missing_python_deps()
    recommended = recommend_hosting(
        is_windows=is_windows,
        has_docker=has_docker,
        in_container=in_container,
        native_deps_missing=bool(missing_deps),
        service_blocked=bool(service_reason),
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
    if missing_deps:
        # In-container the recommendation stays LOCAL, so do not point at the
        # Docker shape there.
        remedy = (
            "the Docker shape avoids a native install"
            if has_docker and not in_container
            else "install the project's Python dependencies before a native launch"
        )
        notes.append(
            f"Backend Python packages are missing ({', '.join(missing_deps)}); "
            f"{remedy}."
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
        browser_blocked_reason=browser_reason,
        missing_python_deps=missing_deps,
        deep=deep,
    )


__all__ = [
    "EnvironmentReport",
    "HostingGate",
    "browser_launch_blocked_reason",
    "detect_environment",
    "docker_available",
    "docker_compose_available",
    "docker_daemon_running",
    "free_disk_gb",
    "hosting_gates",
    "identify_port_owner",
    "list_nymeria_containers",
    "missing_python_deps",
    "port_free",
    "recommend_hosting",
    "service_manager_block",
    "stack_resource_warnings",
    "suggest_free_port",
    "total_ram_gb",
]
