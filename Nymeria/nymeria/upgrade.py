"""``nymeria upgrade``: upgrade the installed package, then restart what runs it (#353).

The documented upgrade line (``uv tool upgrade nymeriaos``) is unsafe on
Windows while anything runs from the tool environment: the ``nymeria.exe``
trampoline waits on the interpreter, Windows will not replace a running
``python.exe`` or a loaded ``.pyd``, and uv removes the environment anyway when
the replace fails, leaving a half-deleted install (#101 entry 36). A tester who
installed the logon task never knows to stop it first. This command knows.

Shapes (read from the marker file each manager leaves at the environment
root, so a relocated ``UV_TOOL_DIR`` or ``PIPX_HOME`` still resolves):

- uv tool (``uv-receipt.toml``), published package: ``uv tool upgrade
  nymeriaos`` (uv keeps the extras and the index recorded in the receipt).
- pipx (``pipx_metadata.json``): ``pipx upgrade nymeriaos``.
- uv tool, editable checkout: refused. An editable install tracks its source
  tree; the upgrade is ``git pull`` there, then a restart.
- Anything else (a venv, a system pip): refused with the pip line, because
  this process cannot know which interpreter the user means to change; inside
  a container, refused with the image/checkout route instead.

``--add-extra EXTRA`` (#422) adds optional extras through the same stop,
install, restart flow: ``uv tool install --upgrade`` with the receipt's
target plus the new extras (other extras, index, and an editable source
kept), so it works for an editable install too. The setup wizard names it
where it cannot install an extra itself (a Windows uv tool install).

On Linux and macOS the upgrade runs in place (replacing files under a running
process is safe there) and the background service restarts AFTER it, because a
long-lived process that lazily imports freshly written modules mixes code
generations (#101 entry 26). Whether anything changed is read from the
environment's ``*.dist-info`` names before and after, so a dependency-only
upgrade restarts too and a no-op one does not.

On Windows it never runs from inside the tool environment. A PowerShell script
written to the temp dir, launched in a new console (uv's trampoline job sets
``JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK``, so the console outlives this
process), waits for this process to exit, then stops EVERY process running
from the environment. Three layers run per ``nymeria`` command (the
trampoline, the venv's ``python.exe`` launcher, and the base interpreter in
uv's Python dir, which holds the locks), and every layer's command line names
the shim, so the match is on command line as well as executable. The stop is
``Stop-Process -Force`` (TerminateProcess): a hidden console process cannot be
signalled cleanly on Windows and the API has no stop endpoint, so the
backend's own teardown (owned children, in-flight writes) does not run; the
stores it could tear are the ones ``core/store_repair.py`` repairs. The script
re-scans until nothing matches (bounded; it refuses to upgrade over a survivor,
which is exactly the half-deleted case), upgrades, and restarts install.ps1's
logon task if a backend (``slim`` or ``api``) had been running.

Every spawn gets a scrubbed environment: ``run.py`` loads the deployment
``.env`` into this process, and none of it belongs in a package build.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

PACKAGE = "nymeriaos"
# install.ps1's `$script:TaskName`: the Windows logon task that runs the backend.
WINDOWS_TASK_NAME = "NymeriaOS Slim"
# The installers' private-index variable (install.sh, install.ps1). uv keeps the
# index in the tool receipt but strips its credentials and marks it
# `authenticate = "always"` (measured, uv 0.11.10), so a private-index install
# only upgrades when the credentialed URL is passed again. It rides in argv,
# as the installers pass it: pipx picks its own backend (pip, or uv when uv is
# on PATH) and translates only its `--index-url` flag, not `PIP_INDEX_URL`
# (measured, pipx 1.17.8).
INDEX_ENV = "NYMERIA_PYPI_SIMPLE_INDEX_URL"
_URL_USERINFO = re.compile(r"(?<=://)[^/\s]+@")
UV_TOOL = "uv-tool"
UV_TOOL_EDITABLE = "uv-tool-editable"
PIPX = "pipx"
OTHER = "other"


@dataclass(frozen=True)
class InstallShape:
    kind: str
    prefix: Path
    checkout: Optional[str] = None  # the editable source, for UV_TOOL_EDITABLE
    shim: Optional[str] = None  # the `nymeria` launcher the receipt records

    def describe(self) -> str:
        return {
            UV_TOOL: "uv tool install",
            UV_TOOL_EDITABLE: "editable uv tool install",
            PIPX: "pipx install",
        }.get(self.kind, "Python environment")


def _read_receipt(prefix: Path) -> Optional[dict]:
    try:
        data = tomllib.loads((prefix / "uv-receipt.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    tool = data.get("tool")
    return tool if isinstance(tool, dict) else None


def _pipx_main_package(prefix: Path) -> Optional[str]:
    try:
        data = json.loads((prefix / "pipx_metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    main = data.get("main_package") if isinstance(data, dict) else None
    return main.get("package") if isinstance(main, dict) else None


def detect_install(prefix: Optional[Path] = None) -> InstallShape:
    """How the running package was installed, from its environment's marker files."""
    prefix = Path(prefix or sys.prefix)
    tool = _read_receipt(prefix)
    if tool is not None:
        requirement = next(
            (
                req
                for req in tool.get("requirements") or []
                if isinstance(req, dict) and req.get("name") == PACKAGE
            ),
            None,
        )
        # A receipt for some other tool (nymeriaos pulled in via --with) is not
        # an install `uv tool upgrade nymeriaos` can act on.
        if requirement is not None:
            shim = next(
                (
                    str(entry["install-path"])
                    for entry in tool.get("entrypoints") or []
                    if isinstance(entry, dict)
                    and entry.get("name") == "nymeria"
                    and entry.get("install-path")
                ),
                None,
            )
            if requirement.get("editable"):
                return InstallShape(
                    UV_TOOL_EDITABLE, prefix, checkout=str(requirement["editable"]), shim=shim
                )
            return InstallShape(UV_TOOL, prefix, shim=shim)
    if _pipx_main_package(prefix) == PACKAGE:
        return InstallShape(PIPX, prefix)
    return InstallShape(OTHER, prefix)


def upgrade_argv(
    shape: InstallShape,
    which: Callable[[str], Optional[str]] = shutil.which,
    index_url: Optional[str] = None,
) -> Optional[list[str]]:
    """The upgrade command for ``shape``, or None when this command must not run one."""
    if shape.kind == UV_TOOL:
        uv = which("uv")
        if not uv:
            return None
        return [uv, "tool", "upgrade", PACKAGE] + (["--index", index_url] if index_url else [])
    if shape.kind == PIPX:
        pipx = which("pipx")
        if not pipx:
            return None
        return [pipx, "upgrade", PACKAGE] + (["--index-url", index_url] if index_url else [])
    return None


def _normalize_extra(name: str) -> str:
    """PEP 685 extra normalization, as ``Provides-Extra`` spells them."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _with_index(target: list[str], index_url: Optional[str]) -> list[str]:
    """``target`` with the credentialed ``index_url`` in place of the receipt's
    credential-stripped copy of it (same flag kind), else added as ``--index``."""
    if not index_url:
        return target
    bare = _URL_USERINFO.sub("", index_url).rstrip("/")
    flag, rest, i = "--index", [], 0
    while i < len(target):
        if target[i] in ("--index", "--default-index") and i + 1 < len(target):
            if target[i + 1].rstrip("/") == bare:
                flag = target[i]
            else:
                rest += target[i : i + 2]
            i += 2
            continue
        rest.append(target[i])
        i += 1
    return [flag, index_url, *rest]


def install_argv(
    shape: InstallShape,
    extras: Sequence[str],
    which: Callable[[str], Optional[str]] = shutil.which,
    index_url: Optional[str] = None,
) -> Optional[list[str]]:
    """``uv tool install --upgrade`` adding ``extras`` to a uv tool install, or None.

    No ``--force``: uv syncs the environment in place and records the extras
    in the receipt; a rebuild would delete the environment under this very
    process (the wizard's rule, ``setup/local_rag_install.py``).
    """
    if shape.kind not in (UV_TOOL, UV_TOOL_EDITABLE):
        return None
    uv = which("uv")
    if not uv:
        return None
    from .setup.local_rag_install import uv_tool_install_target

    target = uv_tool_install_target(shape.prefix, list(extras))
    if target is None:
        return None
    return [uv, "tool", "install", "--upgrade", *_with_index(target, index_url)]


def redact(text: str) -> str:
    """``text`` with any URL's ``user:password@`` replaced, for printing and logs."""
    return _URL_USERINFO.sub("***@", text)


def _display(argv: Sequence[str]) -> str:
    return redact(" ".join(argv))


def child_env(index_url: Optional[str] = None) -> dict[str, str]:
    """The environment every upgrade spawn gets.

    The scrubbed base plus the network passthrough (proxies, CA bundles, XDG
    dirs), the package managers' own settings, ``PSModulePath`` (the Windows
    script's ``Get-CimInstance`` lives in a ``$PSHOME`` module), and the index
    URL when one is in play. Never the deployment secrets ``run.py`` loaded.
    """
    from .subprocess_env import (
        NETWORK_RUNTIME_PASSTHROUGH,
        package_manager_env_names,
        scrubbed_subprocess_env,
    )

    names = [*NETWORK_RUNTIME_PASSTHROUGH, "PSModulePath", *package_manager_env_names()]
    env = scrubbed_subprocess_env(names)
    if index_url:
        env[INDEX_ENV] = index_url
    return env


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_windows_script(
    argv: Sequence[str],
    *,
    wait_pids: Sequence[int],
    env_prefix: Path,
    shim: Optional[str],
    task_name: str = WINDOWS_TASK_NAME,
    log_path: Path,
    env_refs: Optional[dict[str, str]] = None,
) -> str:
    """The PowerShell script that upgrades from OUTSIDE the tool environment.

    ``env_refs`` maps an argv value to the environment variable that holds it:
    that part renders as ``$env:NAME`` (``child_env`` puts it in the child
    console's environment), so a credentialed index URL is never written into
    the script file or echoed into its transcript.
    """
    refs = env_refs or {}
    command = " ".join(f"$env:{refs[part]}" if part in refs else _ps_quote(part) for part in argv)
    pids = ", ".join(str(int(pid)) for pid in wait_pids)
    shim_line = f"$shim = {_ps_quote(shim)}\n" if shim else "$shim = $null\n"
    return (
        "# Generated by `nymeria upgrade` (#353). Safe to delete.\n"
        f"Start-Transcript -Path {_ps_quote(str(log_path))} -Append | Out-Null\n"
        f"$envDir = {_ps_quote(str(env_prefix))}\n"
        "$envRoot = $envDir.TrimEnd('\\', '/') + '\\'\n"
        f"{shim_line}"
        "$ci = [System.StringComparison]::OrdinalIgnoreCase\n"
        "# The trampoline IS the shim and the venv launcher runs from the env; the\n"
        "# base interpreter (uv's Python dir, it holds the locks) is found by its\n"
        "# command line naming either. Only python processes get that rule, so an\n"
        "# editor or shell whose command line mentions the env is left alone.\n"
        "function Get-NymeriaProcesses {\n"
        "    @(Get-CimInstance Win32_Process | Where-Object {\n"
        "        $p = $_\n"
        "        $p.ProcessId -ne $PID -and (\n"
        "            ($p.ExecutablePath -and ($p.ExecutablePath.StartsWith($envRoot, $ci) -or\n"
        "                ($shim -and $p.ExecutablePath -ieq $shim))) -or\n"
        "            ($p.Name -match '^pythonw?[\\d.]*\\.exe$' -and $p.CommandLine -and ($p.CommandLine.IndexOf($envRoot, $ci) -ge 0 -or\n"
        "                ($shim -and $p.CommandLine.IndexOf($shim, $ci) -ge 0)))\n"
        "        )\n"
        "    })\n"
        "}\n"
        "Write-Host 'Waiting for the nymeria command that started this upgrade to exit...'\n"
        f"foreach ($id in @({pids})) {{ Wait-Process -Id $id -Timeout 120 -ErrorAction SilentlyContinue }}\n"
        "$found = Get-NymeriaProcesses\n"
        "# Only a backend is restarted afterwards; a `nymeria cli` window is not.\n"
        "$backend = @($found | Where-Object { $_.CommandLine -match 'nymeria(\\.exe)?\"?\\s+(slim|api)(\\s|$)' }).Count -gt 0\n"
        "if ($found.Count -gt 0) {\n"
        "    Write-Host \"Stopping $($found.Count) running NymeriaOS process(es) so the upgrade can replace their files.\"\n"
        "}\n"
        "$deadline = (Get-Date).AddSeconds(30)\n"
        "while ($found.Count -gt 0 -and (Get-Date) -lt $deadline) {\n"
        "    $found | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }\n"
        "    Start-Sleep -Milliseconds 500\n"
        "    $found = Get-NymeriaProcesses\n"
        "}\n"
        "if ($found.Count -gt 0) {\n"
        "    Write-Host 'Some NymeriaOS processes would not stop, so the upgrade did not run (it would leave a half-deleted install). Close them, then run: nymeria upgrade' -ForegroundColor Red\n"
        "    $found | ForEach-Object { Write-Host \"  $($_.ProcessId) $($_.Name)\" }\n"
        "    Stop-Transcript | Out-Null\n"
        "    Read-Host 'Press Enter to close this window'\n"
        "    exit 1\n"
        "}\n"
        f"Write-Host {_ps_quote('Running: ' + _display(argv))}\n"
        "# 2>&1 so uv's own report (it writes to stderr) reaches the transcript.\n"
        f"& {command} 2>&1 | ForEach-Object {{ Write-Host \"$_\" }}\n"
        "$code = $LASTEXITCODE\n"
        "if ($code -ne 0) {\n"
        "    Write-Host \"The upgrade failed (exit $code). Run the command above again once nothing else uses NymeriaOS.\" -ForegroundColor Red\n"
        "} elseif ($shim) {\n"
        "    & $shim --version\n"
        "}\n"
        "if ($backend) {\n"
        f"    schtasks /query /tn {_ps_quote(task_name)} *> $null\n"
        "    if ($LASTEXITCODE -eq 0) {\n"
        f"        schtasks /run /tn {_ps_quote(task_name)} | Out-Null\n"
        f"        Write-Host {_ps_quote(f'Restarted the {task_name} logon task.')}\n"
        "    } else {\n"
        "        Write-Host 'A NymeriaOS backend was running; start it again with: nymeria slim'\n"
        "    }\n"
        "}\n"
        "Stop-Transcript | Out-Null\n"
        "Read-Host 'Press Enter to close this window'\n"
    )


def installed_distributions() -> frozenset[str]:
    """The ``*.dist-info`` names (``name-version.dist-info``) in this environment.

    Read before and after the upgrade: any difference, the package's own
    version or only a dependency's, means the running backend holds stale code.
    """
    # The site dirs this interpreter imports from (a uv or pipx venv has one),
    # plus the scheme's own paths for an interpreter that pruned sys.path.
    sites = {entry for entry in sys.path if Path(entry).name in ("site-packages", "dist-packages")}
    sites.update(filter(None, (sysconfig.get_path(key) for key in ("purelib", "platlib"))))
    names: set[str] = set()
    for site in sites:
        try:
            names.update(path.name for path in Path(site).glob("*.dist-info"))
        except OSError:
            continue
    return frozenset(names)


def _package_version(distributions: frozenset[str]) -> Optional[str]:
    prefix, suffix = f"{PACKAGE}-", ".dist-info"
    for name in distributions:
        if name.lower().startswith(prefix) and name.endswith(suffix):
            return name[len(prefix) : -len(suffix)]
    return None


@dataclass
class UpgradeDeps:
    """Seams over the host (the suite replaces every one)."""

    platform: str = sys.platform
    prefix: Optional[Path] = None
    which: Callable[[str], Optional[str]] = shutil.which
    run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run
    popen: Callable[..., object] = subprocess.Popen
    isatty: Callable[[], bool] = lambda: sys.stdin.isatty()
    ask: Callable[[str], str] = input
    temp_dir: Callable[[], str] = tempfile.gettempdir
    getenv: Callable[[str], Optional[str]] = os.environ.get
    snapshot: Callable[[], frozenset[str]] = installed_distributions
    in_container: Optional[Callable[[], bool]] = None
    known_extras: Optional[Callable[[], Optional[frozenset[str]]]] = None
    active_service: Optional[Callable[[], object]] = None
    backend_answering: Optional[Callable[[], bool]] = None


def _default_in_container() -> bool:
    from .service_install import _in_container

    return _in_container()


def _default_known_extras() -> Optional[frozenset[str]]:
    """The extras the installed package declares, or None when unreadable."""
    from importlib import metadata

    try:
        declared = metadata.metadata(PACKAGE).get_all("Provides-Extra") or []
    except metadata.PackageNotFoundError:
        return None
    return frozenset(_normalize_extra(name) for name in declared) or None


def _default_active_service() -> object:
    """The background-service manager when the backend service is running, else None."""
    from .service_install import (
        ServiceInstallError,
        ServiceUnavailableError,
        installed_artifact_path,
        service_manager,
    )

    artifact = installed_artifact_path()
    if artifact is None:
        return None
    try:
        # One backend unit per box, but a box can carry several installs (a dev
        # checkout beside a published one): only a unit whose command line runs
        # THIS environment's interpreter runs the code just upgraded.
        # With the separator: `.../nymeriaos` must not match `.../nymeriaos-dev`.
        if f"{Path(sys.prefix)}{os.sep}" not in artifact.read_text(encoding="utf-8", errors="replace"):
            return None
        manager = service_manager()
        return manager if manager.status().running else None
    except (ServiceUnavailableError, ServiceInstallError, OSError):
        return None


def _default_backend_answering() -> bool:
    from .service_install import default_health_url, probe_health

    try:
        return probe_health(default_health_url())
    except Exception:  # noqa: BLE001 - a probe must never break the upgrade
        return False


def _confirm(deps: UpgradeDeps, yes: bool) -> bool:
    if yes:
        return True
    if not deps.isatty():
        print("Not a terminal: pass --yes to upgrade without a prompt.")
        return False
    answer = deps.ask("Proceed? [Y/n] ").strip().lower()
    return answer in ("", "y", "yes")


def upgrade_cli(
    *,
    yes: bool = False,
    dry_run: bool = False,
    add_extras: Sequence[str] = (),
    deps: Optional[UpgradeDeps] = None,
) -> int:
    """Back ``nymeria upgrade``. Returns the process exit code."""
    from . import __version__

    deps = deps or UpgradeDeps()
    shape = detect_install(deps.prefix)
    print(f"NymeriaOS {__version__} ({shape.describe()} at {shape.prefix})")

    extras = list(dict.fromkeys(_normalize_extra(name) for name in add_extras))
    if extras:
        known = (deps.known_extras or _default_known_extras)()
        if known is None:
            # uv accepts an unknown extra with only a warning and records it,
            # so never pass one through unchecked.
            print(f"Could not read which extras {PACKAGE} declares, so --add-extra cannot check them.")
            return 2
        unknown = [name for name in extras if name not in known]
        if unknown:
            print(f"{PACKAGE} has no {', '.join(unknown)} extra.")
            print(f"  Available: {', '.join(sorted(known))}")
            return 2
    spec = f'"{PACKAGE}[{",".join(extras)}]"' if extras else PACKAGE

    if shape.kind == UV_TOOL_EDITABLE and not extras:
        print(
            "This is an editable install: it runs the source in "
            f"{shape.checkout}, so upgrading means updating that checkout."
        )
        print(f"  git -C {shape.checkout} pull --ff-only")
        print("Then restart the backend (nymeria service restart, or start nymeria slim again).")
        return 2
    if shape.kind == OTHER:
        if (deps.in_container or _default_in_container)():
            print(
                "This runs inside a container, whose image carries NymeriaOS, so it is "
                "upgraded by updating the image, not from in here:"
            )
            print("  published image: docker compose pull, then docker compose up -d")
            print("  built from a checkout: git pull there, then docker compose up -d --build")
            return 2
        print(
            "This is not a uv tool or pipx install, so this command will not guess "
            "which environment to change. Upgrade it the way it was installed, e.g.:"
        )
        print(f"  {sys.executable} -m pip install --upgrade {spec}")
        return 2
    if shape.kind == PIPX and extras:
        print("pipx cannot add an extra in place; reinstall with it (name any extras added before, too):")
        print(f"  pipx install --force {spec}")
        return 2

    index_url = deps.getenv(INDEX_ENV) or None
    if extras:
        argv = install_argv(shape, extras, deps.which, index_url)
        if argv is None and deps.which("uv"):
            # The receipt records something a rebuilt requirement cannot carry
            # (a directory, git, or url source, constraints, or uv options
            # beyond an index); uv would silently drop it, so do not guess.
            print(
                f"This install's uv receipt ({shape.prefix / 'uv-receipt.toml'}) records settings "
                "this command cannot carry over (a local, git, or URL source, constraints, or uv "
                "options beyond an index), so it will not rebuild the install for you."
            )
            print(
                "  Add the extra by hand, repeating how you installed NymeriaOS plus the extra, "
                f"with nothing running: uv tool install --upgrade <your options> {spec}"
            )
            return 2
    else:
        argv = upgrade_argv(shape, deps.which, index_url)
    if argv is None:
        tool = "pipx" if shape.kind == PIPX else "uv"
        if extras:
            manual = f"uv tool install --upgrade {spec}"
        else:
            manual = f"uv tool upgrade {PACKAGE}" if tool == "uv" else f"pipx upgrade {PACKAGE}"
        if index_url:
            manual += f' {"--index-url" if tool == "pipx" else "--index"} "${INDEX_ENV}"'
        print(f"`{tool}` is not on PATH, so the upgrade cannot run from here.")
        print(f"  Open a terminal where `{tool}` works and run: {manual}")
        return 2
    if index_url:
        print(f"Using the package index from {INDEX_ENV}.")

    if deps.platform == "win32":
        return _upgrade_windows(argv, shape, yes=yes, dry_run=dry_run, deps=deps, index_url=index_url)
    return _upgrade_in_place(
        argv, yes=yes, dry_run=dry_run, deps=deps, index_url=index_url, extras=extras
    )


def _upgrade_in_place(
    argv: list[str],
    *,
    yes: bool,
    dry_run: bool,
    deps: UpgradeDeps,
    index_url: Optional[str],
    extras: Sequence[str] = (),
) -> int:
    from . import __version__

    service = (deps.active_service or _default_active_service)()
    foreground = not service and (deps.backend_answering or _default_backend_answering)()
    print(f"Will run: {_display(argv)}")
    if service is not None:
        print("Then restart the background service so it runs the new version.")
    if foreground:
        # A port cannot say which install serves it (a box can carry several).
        print(
            "A backend answers on the configured port outside the background service; "
            "if it runs this install, restart it after the upgrade."
        )
    if dry_run:
        print("Dry run: nothing changed.")
        return 0
    if not _confirm(deps, yes):
        return 1

    before = deps.snapshot()
    result = deps.run(argv, env=child_env(index_url))
    if result.returncode != 0:
        print(
            f"The upgrade failed (exit {result.returncode}). Nothing was restarted; "
            f"anything running keeps version {__version__}."
        )
        return result.returncode

    after = deps.snapshot()
    old, new = _package_version(before), _package_version(after)
    named = f"the {', '.join(extras)} extra{'s' if len(extras) > 1 else ''}"
    if before and before == after:
        suffix = f"; {named} {'were' if len(extras) > 1 else 'was'} already installed" if extras else ""
        print(f"Already up to date ({old or __version__}){suffix}.")
        return 0
    if not before and not after:
        # Could not read the environment: assume it changed, restarting is safe.
        print("Upgrade finished (run nymeria --version to see the version).")
    elif new and new != old:
        print(f"Upgraded {PACKAGE} {old or __version__} -> {new}.")
    elif not extras:
        print(f"{PACKAGE} {new or old or __version__} is current; some of its dependencies were upgraded.")
    if extras:
        print(f"Added {named}.")

    if service is not None:
        try:
            service.restart()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - report, never mask the upgrade
            print(f"Could not restart the background service: {exc}")
            print("  Restart it by hand: nymeria service restart")
            return 1
        print("Restarted the background service.")
    if foreground:
        print("If the backend on the configured port runs this install, restart it to pick up the new version.")
    return 0


def _upgrade_windows(
    argv: list[str],
    shape: InstallShape,
    *,
    yes: bool,
    dry_run: bool,
    deps: UpgradeDeps,
    index_url: Optional[str] = None,
) -> int:
    print(f"Will run: {_display(argv)}")
    print(
        "On Windows the upgrade cannot run from inside the install it replaces, so it "
        "continues in a new window once this command exits: it stops every running "
        "NymeriaOS process (the backend and any other nymeria window), upgrades, and "
        f"restarts the '{WINDOWS_TASK_NAME}' logon task if a backend had been running."
    )
    if dry_run:
        print("Dry run: nothing changed.")
        return 0
    if not _confirm(deps, yes):
        return 1

    stamp = time.strftime("%Y%m%d-%H%M%S")
    temp = Path(deps.temp_dir())
    script_path = temp / f"nymeria-upgrade-{stamp}.ps1"
    log_path = temp / f"nymeria-upgrade-{stamp}.log"
    script = build_windows_script(
        argv,
        # This interpreter and its parent; the script's re-scan catches any
        # layer still exiting after that.
        wait_pids=[os.getpid(), os.getppid()],
        env_prefix=shape.prefix,
        shim=shape.shim or deps.which("nymeria"),
        log_path=log_path,
        env_refs={index_url: INDEX_ENV} if index_url else None,
    )
    # utf-8-sig: Windows PowerShell 5.1 reads a BOM-less script as ANSI, which
    # would mangle a non-ASCII profile path so the stop step matched nothing.
    script_path.write_text(script, encoding="utf-8-sig")
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
    deps.popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
        creationflags=flags,
        close_fds=True,
        env=child_env(index_url),
    )
    print(f"The upgrade continues in a new window (log: {log_path}). This window can close.")
    return 0
