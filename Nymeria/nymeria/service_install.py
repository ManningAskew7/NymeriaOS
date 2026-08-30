"""Background-service install for the slim backend.

Installs "start on login, keep running" supervision for `nymeria slim`:
a systemd user unit on Linux, a launchd agent on macOS. Consumed by the
setup wizard (`setup/finalize.py`, the Background service hosting choice)
and by the `nymeria service install|uninstall|status|restart` CLI.

Design notes (researched against OpenClaw, hermes-agent, Maestral, and
current systemd/launchd guidance, 2026-06):

- Restart on crash only (`Restart=on-failure`, launchd
  `KeepAlive={SuccessfulExit: false}`): restart-on-everything fights
  deliberate stops. NOTE (#300): the API's own `/restart api` no longer
  depends on this policy either way. It replaces its process image in place
  (`api/routers/system.py::restart_api_process`), so the unit never sees an
  exit and never restarts. It used to spawn a copy and exit, which this
  policy correctly declined to restart, leaving the backend dead. `Type=exec`
  stays safe under an in-place restart; `Type=notify` would need the new
  image to re-send READY=1, and `Type=forking` would break outright. On Linux, start-rate limits park a misconfigured
  backend in `failed` instead of crash-looping forever; launchd has no
  give-up limit, only its default 10s respawn throttle, so a broken
  config on macOS retries indefinitely (the install therefore refuses
  to proceed when no config exists at the root).
- ExecStart uses absolute paths only (service managers do not search the
  user's PATH) and mirrors the wizard's own start pattern:
  `<python> <script> slim`, which works for source checkouts and the
  installed `nymeria` console script alike.
- The unit carries no secrets: only NYMERIA_PROJECT_ROOT and a baked PATH.
  The backend reads config.env from the project root itself.
- `NoNewPrivileges` is deliberately NOT set: the agent's shell tool may
  legitimately run sudo on the user's behalf (NOPASSWD setups; the
  service has no TTY for a password prompt), and that directive would
  break it under the service.
- launchd: write plist, then bootout (best effort, then wait for the
  label to actually drain: bootout is asynchronous and an immediate
  bootstrap can fail with error 5 while the old job winds down), enable
  (clears a sticky disabled flag), bootstrap. No kickstart after install:
  RunAtLoad starts the job, and a `kickstart -k` would SIGTERM the fresh
  process.
- Linger: stock polkit lets a user enable linger for themselves without
  sudo. Failure is a warning, never an abort.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

SYSTEMD_UNIT_NAME = "nymeria.service"
LAUNCHD_LABEL = "com.nymeria.backend"
SERVICE_DESCRIPTION = "Nymeria backend (slim)"

# Subprocess timeout for every service-manager command. Most calls answer
# fast, but `systemctl --user restart` blocks through the stop phase, which
# can take up to the unit's TimeoutStopSec (30s) before SIGKILL plus the
# start; the timeout must clear that with margin or a slow graceful stop
# makes a successful reinstall report failure.
COMMAND_TIMEOUT_SECONDS = 90.0
# Matches TimeoutStopSec in the generated unit; COMMAND_TIMEOUT_SECONDS must
# stay comfortably above it (see test_command_timeout_clears_stop_timeout).
UNIT_STOP_TIMEOUT_SECONDS = 30

_SYSTEMD_HEADLESS_HINTS = (
    "Enable lingering so your user manager runs without a login session: "
    "sudo loginctl enable-linger $USER",
    "Ensure the user bus env is set: export XDG_RUNTIME_DIR=/run/user/$(id -u)",
)
_WSL_HINTS = (
    "WSL2 needs systemd enabled: add [boot] systemd=true to /etc/wsl.conf, "
    "then run `wsl --shutdown` from PowerShell and reopen the distro.",
    "Note: WSL distros still shut down when idle, so a service inside WSL "
    "does not keep the machine reachable on its own.",
)
_WINDOWS_HINTS = (
    "Windows has no automated `nymeria service` backend yet. For logon "
    "autostart, run install.ps1: it registers a hidden 'NymeriaOS Slim' "
    "scheduled task that launches `nymeria slim` at login with no console "
    "window.",
    "Start it now without logging out: schtasks /run /tn \"NymeriaOS Slim\". "
    "Remove it: schtasks /delete /tn \"NymeriaOS Slim\" /f.",
    "For crash-restart supervision, see WinSW: https://github.com/winsw/winsw",
)


class ServiceUnavailableError(RuntimeError):
    """No background-service manager is usable here; hints say what to do."""

    def __init__(self, reason: str, hints: Sequence[str] = ()) -> None:
        super().__init__(reason)
        self.hints: tuple[str, ...] = tuple(hints)


class ServiceInstallError(RuntimeError):
    """A service-manager command failed; the message carries the detail."""


@dataclass(frozen=True)
class InstallReport:
    """What an install did, for the caller to print."""

    artifact: Path
    lines: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServiceStatus:
    installed: bool
    running: bool
    detail: str
    pid: int | None = None


@dataclass(frozen=True)
class _RunResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


# --- shared helpers ----------------------------------------------------------


def resolve_exec_argv(args: Sequence[str] = ("slim",)) -> list[str]:
    """The absolute-path command that re-runs this entry point with ``args``.

    Mirrors finalize's foreground-start pattern so source checkouts
    (`python3 run.py ...`) and installed console scripts (`nymeria ...`)
    both produce a command that outlives the install shell.

    Two callers, same problem. The service install bakes it into a unit or
    plist and wants the default (the slim backend). The API's in-place
    self-restart (`api/routers/system.py::restart_api_process`) passes its OWN
    `sys.argv[1:]` to re-run whatever this process was started as. Both need
    the branches below rather than a bare `[sys.executable] + sys.argv`, which
    is wrong for a `-m` launch and for a frozen build.
    """
    tail = list(args)
    if getattr(sys, "frozen", False):
        # sys.executable IS the app, so argv[0] must not be repeated after it.
        return [sys.executable, *tail]
    # A `python -m pkg.mod` launch sets argv[0] to the module's FILE path,
    # which cannot be re-run as a script (relative imports fail at once),
    # so reproduce the -m invocation instead of trusting argv[0].
    main_spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    if main_spec is not None and getattr(main_spec, "name", None):
        module = main_spec.name.removesuffix(".__main__")
        return [sys.executable, "-m", module, *tail]
    script = Path(os.path.abspath(sys.argv[0]))
    if script.is_file():
        return [sys.executable, str(script), *tail]
    console_script = shutil.which("nymeria")
    if console_script:
        return [console_script, *tail]
    return [sys.executable, "-m", "nymeria.cli_entry", *tail]


# Path fragments that mark throwaway environments (uvx / pipx run caches):
# a unit baked against one breaks whenever the cache is pruned.
_EPHEMERAL_PATH_HINTS = ("/.cache/uv/", "/uv/cache/", "/pipx/.cache/", "/pipx/run/")


def ephemeral_exec_warning(exec_argv: Sequence[str]) -> str | None:
    """A warning when the command lives in a prunable cache env, else None."""
    for part in exec_argv:
        normalized = part.replace("\\", "/")
        if any(hint in normalized for hint in _EPHEMERAL_PATH_HINTS):
            return (
                f"The service command points into a temporary environment "
                f"({part}). It breaks when that cache is pruned; install "
                "Nymeria persistently (uv tool install / pipx install) and "
                "re-run `nymeria service install`."
            )
    return None


def service_path_env() -> str:
    """A baked PATH for the service: managers give units a bare default.

    The interpreter's own bin dir comes first (venv-installed console
    scripts), then the common per-user and system dirs the backend's shell
    tools expect.
    """
    parts: list[str] = [str(Path(sys.executable).parent)]
    optional = [Path.home() / ".local" / "bin"]
    if sys.platform == "darwin":
        optional.append(Path("/opt/homebrew/bin"))
    parts.extend(str(p) for p in optional if p.is_dir())
    parts.extend(["/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    seen: dict[str, None] = {}
    for part in parts:
        seen.setdefault(part, None)
    return ":".join(seen)


def probe_health(url: str, timeout: float = 2.0) -> bool:
    """One GET against the backend health endpoint; True on any 2xx."""
    import http.client
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return 200 <= getattr(resp, "status", 200) < 300
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        # HTTPException covers a non-HTTP listener squatting on the port
        # (BadStatusLine escapes urllib's own OSError wrapping).
        return False


def wait_for_backend_health(
    url: str, *, timeout: float = 40.0, interval: float = 1.0
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe_health(url):
            return True
        time.sleep(interval)
    return False


def _read_env_port(root: Path) -> int | None:
    """API_PORT from the root's config files, highest precedence last.

    A tiny local parser (quotes stripped, comments skipped) instead of a
    dotenv/setup import: this module must stay importable with zero deps.
    """
    port: int | None = None
    for name in (".env", "config.env", ".env.docker"):
        try:
            lines = (root / name).read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if not stripped.startswith("API_PORT="):
                continue
            value = stripped.split("=", 1)[1].strip().strip("'\"")
            if value.isdigit():
                port = int(value)
    return port


def default_health_url(root: Path | None = None) -> str:
    """The health URL the installed service actually answers on.

    The unit runs bare `... slim` with NYMERIA_PROJECT_ROOT set, and slim
    resolves its port from the config that root points at (explicit --port >
    API_PORT > 8000; see run.py `_resolve_slim_port`), so the truthful probe
    target follows the root's API_PORT when a root is known.
    """
    port = (_read_env_port(root) if root is not None else None) or 8000
    return f"http://127.0.0.1:{port}/health"


# Host markers, module-level so tests can point them at temp paths.
_CONTAINER_MARKERS = (Path("/.dockerenv"), Path("/run/.containerenv"))
_SYSTEMD_MARKER = Path("/run/systemd/system")
_PROC_VERSION = Path("/proc/version")
_LINGER_DIR = Path("/var/lib/systemd/linger")


def _is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in _PROC_VERSION.read_text().lower()
    except OSError:
        return False


def _in_container() -> bool:
    # /.dockerenv (docker), /run/.containerenv (podman), container= (LXC etc.)
    if os.environ.get("container"):
        return True
    return any(marker.exists() for marker in _CONTAINER_MARKERS)


def _escape_specifiers(value: str) -> str:
    """systemd expands % specifiers in unit values; literal % must double."""
    return value.replace("%", "%%")


def _systemd_quote_arg(arg: str) -> str:
    """Quote one ExecStart argument per systemd command-line parsing.

    Beyond % specifiers, ExecStart performs $VAR/${VAR} expansion even
    inside quotes (literal dollars must double to $$), a bare apostrophe
    opens a quoted section (fatal "Unbalanced quoting"), and an unquoted
    `;` is a command separator; all three must be neutralized.
    """
    if "\n" in arg or "\r" in arg:
        raise ServiceInstallError(
            "service command arguments cannot contain newlines"
        )
    escaped = _escape_specifiers(arg).replace("$", "$$")
    needs_quotes = (
        not escaped
        or any(c.isspace() for c in escaped)
        or any(c in escaped for c in "\"\\';")
    )
    if needs_quotes:
        escaped = escaped.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return escaped


def _systemd_env_line(key: str, value: str) -> str:
    if any(c in value for c in "\n\r") or any(c in key for c in "\n\r="):
        raise ServiceInstallError("service environment values cannot contain newlines")
    escaped = _escape_specifiers(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'Environment="{key}={escaped}"'


def build_systemd_unit(
    *, exec_argv: Sequence[str], root: Path, path_env: str
) -> str:
    """The systemd user unit text for the slim backend."""
    exec_line = " ".join(_systemd_quote_arg(arg) for arg in exec_argv)
    root_str = str(root)
    if "\n" in root_str or "\r" in root_str:
        raise ServiceInstallError("project root path cannot contain newlines")
    lines = [
        "[Unit]",
        f"Description={SERVICE_DESCRIPTION}",
        # Crash-loop guard: five failed starts inside five minutes parks the
        # unit in failed instead of looping forever on a broken config.
        "StartLimitIntervalSec=300",
        "StartLimitBurst=5",
        "",
        "[Service]",
        # exec (vs simple) fails `start` immediately on a bad ExecStart path,
        # the most likely failure for a per-user install.
        "Type=exec",
        f"ExecStart={exec_line}",
        f"WorkingDirectory={_escape_specifiers(root_str)}",
        _systemd_env_line("NYMERIA_PROJECT_ROOT", root_str),
        _systemd_env_line("PATH", path_env),
        "Restart=on-failure",
        "RestartSec=2",
        f"TimeoutStopSec={UNIT_STOP_TIMEOUT_SECONDS}",
        "SyslogIdentifier=nymeria",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def build_launchd_plist(
    *, exec_argv: Sequence[str], root: Path, path_env: str
) -> dict:
    """The launchd agent payload for the slim backend (plistlib-ready)."""
    logs_dir = root / "logs"
    return {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": list(exec_argv),
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {
            "NYMERIA_PROJECT_ROOT": str(root),
            "PATH": path_env,
        },
        "RunAtLoad": True,
        # Restart on crash only; a clean exit stays down. Deliberate stops go
        # through bootout, which removes the job so KeepAlive cannot respawn it.
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": str(logs_dir / "service-stdout.log"),
        "StandardErrorPath": str(logs_dir / "service-stderr.log"),
    }


# --- shared service-manager plumbing -----------------------------------------


class _SubprocessServiceBase:
    """Subprocess plumbing shared by the platform service managers.

    Both managers drive their CLI (systemctl/loginctl, launchctl) through one
    `_run`: capture+text output under COMMAND_TIMEOUT_SECONDS, the
    OSError/TimeoutExpired -> _RunResult(127) translation, and the
    check-raises-ServiceInstallError contract. The single per-manager axis is
    the process env (systemd repairs XDG_RUNTIME_DIR/DBus for headless shells;
    launchd inherits the caller's env), held in `_env` and passed to the runner
    only when set, so the runner call shape stays identical to each manager's
    former hand-rolled copy.
    """

    _runner: Runner
    _env: dict[str, str] | None

    def _run(self, argv: Sequence[str], *, check: bool = False) -> _RunResult:
        run_kwargs: dict[str, object] = {
            "capture_output": True,
            "text": True,
            "timeout": COMMAND_TIMEOUT_SECONDS,
        }
        if self._env is not None:
            run_kwargs["env"] = self._env
        try:
            result = self._runner(list(argv), **run_kwargs)
        except (OSError, subprocess.TimeoutExpired) as exc:
            if check:
                raise ServiceInstallError(f"{argv[0]} failed: {exc}") from exc
            return _RunResult(returncode=127, stderr=str(exc))
        run = _RunResult(
            returncode=result.returncode,
            stdout=(result.stdout or "").strip(),
            stderr=(result.stderr or "").strip(),
        )
        if check and run.returncode != 0:
            detail = run.stderr or run.stdout or f"exit {run.returncode}"
            raise ServiceInstallError(f"{' '.join(argv)} failed: {detail}")
        return run


# --- systemd user service (Linux) --------------------------------------------


class SystemdUserService(_SubprocessServiceBase):
    """Install/manage the slim backend as a systemd user unit."""

    name = "systemd user service"

    def __init__(
        self, *, home: Path | None = None, runner: Runner = subprocess.run
    ) -> None:
        self._home = home or Path.home()
        self._runner = runner
        self._env = self._build_env()

    @property
    def artifact_path(self) -> Path:
        return self._home / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME

    def is_installed(self) -> bool:
        return self.artifact_path.exists()

    def log_hint(self) -> str:
        return f"journalctl --user -u {SYSTEMD_UNIT_NAME} -n 50 --no-pager"

    def _build_env(self) -> dict[str, str]:
        """Process env for systemctl/loginctl, repaired for headless shells.

        `systemctl --user` needs XDG_RUNTIME_DIR and the user bus address;
        sudo/cron/broken-WSL sessions lack them. Injecting the canonical
        values is safe: they only matter if the socket actually exists.
        """
        env = dict(os.environ)
        if not env.get("XDG_RUNTIME_DIR"):
            runtime_dir = f"/run/user/{os.getuid()}"
            env["XDG_RUNTIME_DIR"] = runtime_dir
            env.setdefault(
                "DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus"
            )
        return env

    def ensure_available(self) -> None:
        """Raise ServiceUnavailableError unless `systemctl --user` is usable."""
        if _in_container():
            raise ServiceUnavailableError(
                "this looks like a container; the container runtime is the "
                "service manager (use a restart policy instead)",
            )
        if not _SYSTEMD_MARKER.exists():
            raise ServiceUnavailableError(
                "systemd is not running on this host",
                hints=_WSL_HINTS if _is_wsl() else (),
            )
        probe = self._run(["systemctl", "--user", "is-system-running"])
        state = probe.stdout.splitlines()[0].strip() if probe.stdout else ""
        if state in {"running", "degraded", "starting", "initializing"}:
            return
        detail = probe.stderr or state or "no user service manager"
        hints = list(_SYSTEMD_HEADLESS_HINTS)
        if _is_wsl():
            hints.extend(_WSL_HINTS)
        raise ServiceUnavailableError(
            f"the systemd user manager is not reachable ({detail})",
            hints=hints,
        )

    def _linger_enabled(self) -> bool:
        user = self._username()
        if (_LINGER_DIR / user).exists():
            return True
        result = self._run(["loginctl", "show-user", user, "--property=Linger"])
        return result.returncode == 0 and result.stdout.strip() == "Linger=yes"

    def _username(self) -> str:
        # The real account name for the calling uid: USER/LOGNAME can be stale
        # (`su user` without `-`), and the linger marker file is named after
        # the account, so trusting env would check the wrong user.
        import pwd

        try:
            return pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            return os.environ.get("USER") or os.environ.get("LOGNAME") or str(os.getuid())

    def _ensure_linger(self) -> tuple[list[str], list[str]]:
        """Enable linger so the service survives logout and starts at boot.

        Returns (lines, warnings); never raises. Stock polkit allows a user
        to enable linger for themselves without sudo.
        """
        if self._linger_enabled():
            return (["Lingering already enabled (service survives logout)."], [])
        result = self._run(["loginctl", "enable-linger"])
        if result.returncode == 0 and self._linger_enabled():
            return (["Lingering enabled (service survives logout)."], [])
        detail = result.stderr or result.stdout or f"exit {result.returncode}"
        return (
            [],
            [
                f"Could not enable lingering ({detail}). Without it the "
                "service stops at logout and only starts after you log in.",
                f"Enable it manually with: sudo loginctl enable-linger {self._username()}",
            ],
        )

    def install(self, *, exec_argv: Sequence[str], root: Path) -> InstallReport:
        self.ensure_available()
        unit_text = build_systemd_unit(
            exec_argv=exec_argv, root=root, path_env=service_path_env()
        )
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_path.write_text(unit_text, encoding="utf-8")
        lines = [f"Installed systemd user unit: {self.artifact_path}"]
        self._run(["systemctl", "--user", "daemon-reload"], check=True)
        self._run(["systemctl", "--user", "enable", SYSTEMD_UNIT_NAME], check=True)
        # restart (not `enable --now`) so a reinstall picks up the new unit
        # instead of leaving a stale process running.
        self._run(["systemctl", "--user", "restart", SYSTEMD_UNIT_NAME], check=True)
        lines.append("Service enabled and started.")
        linger_lines, warnings = self._ensure_linger()
        lines.extend(linger_lines)
        ephemeral = ephemeral_exec_warning(exec_argv)
        if ephemeral:
            warnings.append(ephemeral)
        return InstallReport(
            artifact=self.artifact_path,
            lines=tuple(lines),
            warnings=tuple(warnings),
            notes=(f"Logs: {self.log_hint()}",),
        )

    def uninstall(self) -> tuple[str, ...]:
        if not self.is_installed():
            return (f"No systemd user unit at {self.artifact_path}; nothing to remove.",)
        lines: list[str] = []
        stop = self._run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME])
        if stop.returncode != 0:
            detail = stop.stderr or stop.stdout or f"exit {stop.returncode}"
            lines.append(
                f"Warning: could not stop the service ({detail}); the backend "
                "process may still be running."
            )
        self.artifact_path.unlink(missing_ok=True)
        self._run(["systemctl", "--user", "daemon-reload"])
        # Clear a parked-failed state so the removed unit does not linger in
        # `systemctl --user --failed`.
        self._run(["systemctl", "--user", "reset-failed", SYSTEMD_UNIT_NAME])
        lines.append(f"Removed systemd user unit: {self.artifact_path}")
        lines.append("Lingering was left as-is (other user services may rely on it).")
        return tuple(lines)

    def restart(self) -> None:
        self.ensure_available()
        if not self.is_installed():
            raise ServiceInstallError(
                "service is not installed; run `nymeria service install` first"
            )
        self._run(["systemctl", "--user", "restart", SYSTEMD_UNIT_NAME], check=True)

    def status(self) -> ServiceStatus:
        if not self.is_installed():
            return ServiceStatus(
                installed=False, running=False, detail="not installed"
            )
        result = self._run(
            [
                "systemctl",
                "--user",
                "show",
                SYSTEMD_UNIT_NAME,
                "--no-pager",
                "--property=ActiveState,SubState,MainPID",
            ]
        )
        if result.returncode != 0:
            detail = result.stderr or "systemctl --user not reachable"
            return ServiceStatus(installed=True, running=False, detail=detail)
        props = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
        active = props.get("ActiveState", "unknown")
        sub = props.get("SubState", "unknown")
        pid_raw = props.get("MainPID", "0")
        pid = int(pid_raw) if pid_raw.isdigit() and pid_raw != "0" else None
        running = active == "active"
        detail = f"{active} ({sub})" + (f", pid {pid}" if pid else "")
        return ServiceStatus(installed=True, running=running, detail=detail, pid=pid)


# --- launchd agent (macOS) ----------------------------------------------------


class LaunchdAgentService(_SubprocessServiceBase):
    """Install/manage the slim backend as a launchd LaunchAgent."""

    name = "launchd agent"

    def __init__(
        self, *, home: Path | None = None, runner: Runner = subprocess.run
    ) -> None:
        self._home = home or Path.home()
        self._runner = runner
        # launchd commands inherit the caller's environment (no XDG/DBus repair
        # is needed, unlike systemd --user); _env=None tells the base _run to
        # omit the env kwarg, matching the former hand-rolled call exactly.
        self._env = None
        self._uid = os.getuid()

    @property
    def artifact_path(self) -> Path:
        return self._home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"

    def is_installed(self) -> bool:
        return self.artifact_path.exists()

    def _gui_target(self) -> str:
        return f"gui/{self._uid}"

    def log_hint(self) -> str:
        stdout_log = self._stdout_log_path()
        if stdout_log:
            return f"tail -f {stdout_log}"
        return "tail -f <project-root>/logs/service-stdout.log"

    def _stdout_log_path(self) -> str | None:
        """The StandardOutPath of the installed plist, when readable."""
        if not self.is_installed():
            return None
        try:
            with open(self.artifact_path, "rb") as fh:
                payload = plistlib.load(fh)
        except (OSError, plistlib.InvalidFileException):
            return None
        path = payload.get("StandardOutPath")
        return path if isinstance(path, str) else None

    def ensure_available(self) -> None:
        """Raise ServiceUnavailableError unless the GUI launchd domain exists."""
        probe = self._run(["launchctl", "print", self._gui_target()])
        if probe.returncode != 0:
            raise ServiceUnavailableError(
                "no GUI launchd session for this user, so the agent cannot be "
                "started now (LaunchAgents run inside a logged-in macOS "
                "desktop session; this looks like SSH/headless)",
                hints=(
                    "Sign in to the macOS desktop as this user and re-run the install.",
                ),
            )

    def install(self, *, exec_argv: Sequence[str], root: Path) -> InstallReport:
        self.ensure_available()
        payload = build_launchd_plist(
            exec_argv=exec_argv, root=root, path_env=service_path_env()
        )
        (root / "logs").mkdir(parents=True, exist_ok=True)
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.artifact_path, "wb") as fh:
            plistlib.dump(payload, fh)
        lines = [f"Installed launchd agent: {self.artifact_path}"]
        # bootout clears any prior registration (best effort: it fails when
        # the label is not loaded, which is fine); enable clears the sticky
        # disabled flag that survives plist rewrites; bootstrap loads and,
        # via RunAtLoad, starts the job.
        self._run(["launchctl", "bootout", self._gui_target(), str(self.artifact_path)])
        # bootout is asynchronous: bootstrapping while the old job drains
        # fails with "Bootstrap failed: 5: Input/output error". Wait for the
        # label to disappear (bounded), then retry bootstrap once.
        self._wait_for_label_gone()
        self._run(["launchctl", "enable", f"{self._gui_target()}/{LAUNCHD_LABEL}"])
        bootstrap = ["launchctl", "bootstrap", self._gui_target(), str(self.artifact_path)]
        first = self._run(bootstrap)
        if first.returncode != 0:
            time.sleep(1.0)
            self._run(bootstrap, check=True)
        lines.append("Agent loaded and started.")
        ephemeral = ephemeral_exec_warning(exec_argv)
        return InstallReport(
            artifact=self.artifact_path,
            lines=tuple(lines),
            warnings=(ephemeral,) if ephemeral else (),
            notes=(
                "macOS will show a notification that a background item was "
                "added. Nymeria must stay enabled under System Settings > "
                "General > Login Items & Extensions.",
                "A launchd agent runs only while you are logged in to this Mac.",
                f"Logs: {root / 'logs' / 'service-stdout.log'}",
            ),
        )

    def _wait_for_label_gone(self, *, timeout: float = 5.0, interval: float = 0.2) -> None:
        """Poll until launchd no longer knows the label (bootout drained)."""
        target = f"{self._gui_target()}/{LAUNCHD_LABEL}"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._run(["launchctl", "print", target]).returncode != 0:
                return
            time.sleep(interval)

    def uninstall(self) -> tuple[str, ...]:
        if not self.is_installed():
            return (f"No launchd agent at {self.artifact_path}; nothing to remove.",)
        self._run(["launchctl", "bootout", self._gui_target(), str(self.artifact_path)])
        self.artifact_path.unlink(missing_ok=True)
        return (f"Removed launchd agent: {self.artifact_path}",)

    def restart(self) -> None:
        self.ensure_available()
        if not self.is_installed():
            raise ServiceInstallError(
                "service is not installed; run `nymeria service install` first"
            )
        target = f"{self._gui_target()}/{LAUNCHD_LABEL}"
        result = self._run(["launchctl", "kickstart", "-k", target])
        if result.returncode != 0:
            # The job may not be loaded (e.g. booted out earlier): re-register.
            self._run(["launchctl", "enable", target])
            self._run(
                ["launchctl", "bootstrap", self._gui_target(), str(self.artifact_path)],
                check=True,
            )

    def status(self) -> ServiceStatus:
        if not self.is_installed():
            return ServiceStatus(
                installed=False, running=False, detail="not installed"
            )
        result = self._run(
            ["launchctl", "print", f"{self._gui_target()}/{LAUNCHD_LABEL}"]
        )
        if result.returncode != 0:
            return ServiceStatus(
                installed=True, running=False, detail="installed but not loaded"
            )
        pid: int | None = None
        state = "loaded"
        for raw in result.stdout.splitlines():
            line = raw.strip()
            if line.startswith("pid = "):
                tail = line.removeprefix("pid = ").strip()
                if tail.isdigit():
                    pid = int(tail)
            elif line.startswith("state = "):
                state = line.removeprefix("state = ").strip()
        running = pid is not None or state == "running"
        detail = state + (f", pid {pid}" if pid else "")
        return ServiceStatus(installed=True, running=running, detail=detail, pid=pid)


ServiceManager = SystemdUserService | LaunchdAgentService


# --- platform selection -------------------------------------------------------


def service_manager(*, runner: Runner = subprocess.run) -> ServiceManager:
    """The platform's service manager, or ServiceUnavailableError with hints."""
    if sys.platform.startswith("linux"):
        return SystemdUserService(runner=runner)
    if sys.platform == "darwin":
        return LaunchdAgentService(runner=runner)
    if sys.platform == "win32":
        raise ServiceUnavailableError(
            "background-service install is not automated on Windows yet",
            hints=_WINDOWS_HINTS,
        )
    raise ServiceUnavailableError(
        f"background-service install is not supported on {sys.platform}",
    )


def installed_artifact_path() -> Path | None:
    """The installed unit/plist path, if one exists. Cheap, never raises.

    Used by setup hydration to recover the SERVICE hosting choice from disk.
    """
    if sys.platform.startswith("linux"):
        path = Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    else:
        return None
    return path if path.exists() else None


# --- CLI ----------------------------------------------------------------------


def service_cli(action: str, *, root: Path | None = None) -> int:
    """Back `nymeria service install|uninstall|status|restart`. Returns exit code."""
    if root is None:
        from nymeria._runtime_paths import configure_project_root

        root = configure_project_root()

    try:
        manager = service_manager()
    except ServiceUnavailableError as exc:
        print(f"Background service unavailable: {exc}")
        for hint in exc.hints:
            print(f"  - {hint}")
        print("Run the backend in the foreground instead: nymeria slim")
        return 2

    if action == "install":
        return _cli_install(manager, root)
    if action == "uninstall":
        try:
            for line in manager.uninstall():
                print(line)
        except (ServiceInstallError, OSError) as exc:
            print(f"Uninstall failed: {exc}")
            return 1
        return 0
    if action == "restart":
        try:
            manager.restart()
        except (ServiceUnavailableError, ServiceInstallError) as exc:
            print(f"Restart failed: {exc}")
            for hint in getattr(exc, "hints", ()):
                print(f"  - {hint}")
            return 1
        print("Service restarted.")
        return 0
    if action == "status":
        return _cli_status(manager, root)
    print(f"Unknown service action: {action}")
    return 2


def _config_exists(root: Path) -> bool:
    return any((root / name).exists() for name in (".env", "config.env", ".env.docker"))


def _cli_install(manager: ServiceManager, root: Path) -> int:
    if not _config_exists(root):
        # Without a config the unit would just crash-loop (and on macOS,
        # launchd never gives up); refuse with the actual fix instead.
        print(f"No Nymeria configuration found at {root}.")
        print("Run `nymeria init` first, then install the service.")
        return 2
    try:
        report = manager.install(exec_argv=resolve_exec_argv(), root=root)
    except ServiceUnavailableError as exc:
        print(f"Cannot install a background service here: {exc}")
        for hint in exc.hints:
            print(f"  - {hint}")
        print("Run the backend in the foreground instead: nymeria slim")
        return 2
    except (ServiceInstallError, OSError) as exc:
        print(f"Service install failed: {exc}")
        return 1
    for line in report.lines:
        print(line)
    for warning in report.warnings:
        print(f"Warning: {warning}")
    for note in report.notes:
        print(note)
    url = default_health_url(root)
    print("Waiting for the backend to become healthy...")
    if wait_for_backend_health(url):
        print(f"Backend is up: {url}")
        return 0
    print(
        "Installed, but the backend has not answered its health check yet. "
        f"Check: {manager.log_hint()}"
    )
    return 1


def _cli_status(manager: ServiceManager, root: Path) -> int:
    status = manager.status()
    print(f"{manager.name}: {status.detail} ({manager.artifact_path})")
    if not status.installed:
        print("Install it with: nymeria service install")
        return 1
    url = default_health_url(root)
    if probe_health(url):
        print(f"Backend health: ok ({url})")
        return 0
    print(f"Backend health: not answering ({url})")
    if status.running:
        print(f"The service is running but the API is not up yet. Logs: {manager.log_hint()}")
    else:
        print("Start it with: nymeria service restart")
    return 1


__all__ = [
    "COMMAND_TIMEOUT_SECONDS",
    "InstallReport",
    "LAUNCHD_LABEL",
    "LaunchdAgentService",
    "SERVICE_DESCRIPTION",
    "SYSTEMD_UNIT_NAME",
    "ServiceInstallError",
    "ServiceManager",
    "ServiceStatus",
    "ServiceUnavailableError",
    "SystemdUserService",
    "UNIT_STOP_TIMEOUT_SECONDS",
    "build_launchd_plist",
    "build_systemd_unit",
    "default_health_url",
    "ephemeral_exec_warning",
    "installed_artifact_path",
    "probe_health",
    "resolve_exec_argv",
    "service_cli",
    "service_manager",
    "service_path_env",
    "wait_for_backend_health",
]
