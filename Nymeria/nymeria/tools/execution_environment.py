"""Runtime execution environment helpers for shell and file tools."""

from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from langchain_core.tools import BaseTool

from ..config import get_settings


_DESCRIPTION_BASES: dict[str, str] = {}
_SHELL_TOOL_NAMES = {"bash_execute"}
_FILE_TOOL_NAMES = {"file_read", "file_write", "file_edit"}


@dataclass(frozen=True)
class ExecutionEnvironment:
    """Detected runtime context exposed to tool descriptions."""

    platform_label: str
    default_cwd: str
    process_cwd: str
    shell_executable: str
    available_shells: tuple[str, ...]
    in_container: bool
    path_separator: str


def default_tool_cwd() -> Path:
    """Return the default cwd used by shell and relative file paths."""
    try:
        return get_settings().project_root.resolve()
    except Exception:
        return Path.cwd().resolve()


def resolve_tool_path(raw_path: str | os.PathLike[str]) -> Path:
    """Resolve absolute or relative tool paths against ``default_tool_cwd``."""
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = default_tool_cwd() / path
    return path.resolve()


def resolve_tool_working_directory(working_directory: str | None) -> Path:
    """Resolve an optional working directory against ``default_tool_cwd``."""
    if working_directory is None or str(working_directory).strip() == "":
        return default_tool_cwd()
    return resolve_tool_path(working_directory)


@lru_cache(maxsize=1)
def detect_execution_environment() -> ExecutionEnvironment:
    """Detect shell/runtime facts once per process."""
    system = platform.system() or os.name
    release = platform.release()
    platform_label = f"{system} {release}".strip()
    default_cwd = str(default_tool_cwd())
    process_cwd = str(Path.cwd().resolve())

    candidates = ("bash", "sh", "zsh", "fish", "pwsh", "powershell", "cmd")
    available_shells = tuple(name for name in candidates if shutil.which(name))

    if os.name == "nt":
        shell_executable = os.environ.get("COMSPEC") or "cmd.exe"
    else:
        shell_executable = shutil.which("sh") or "/bin/sh"

    return ExecutionEnvironment(
        platform_label=platform_label,
        default_cwd=default_cwd,
        process_cwd=process_cwd,
        shell_executable=shell_executable,
        available_shells=available_shells,
        in_container=_detect_container(),
        path_separator=os.sep,
    )


def reset_execution_environment_cache() -> None:
    """Clear cached detection results for tests."""
    detect_execution_environment.cache_clear()


def configure_environment_aware_tool_descriptions(
    tools: Iterable[BaseTool],
    env: ExecutionEnvironment | None = None,
) -> None:
    """Patch selected tool descriptions with the current execution profile."""
    env = env or detect_execution_environment()
    for tool in tools:
        name = getattr(tool, "name", "")
        if name not in _SHELL_TOOL_NAMES and name not in _FILE_TOOL_NAMES:
            continue
        base = _DESCRIPTION_BASES.setdefault(name, (tool.description or "").strip())
        if name in _SHELL_TOOL_NAMES:
            tool.description = _shell_description(base, env)
        elif name in _FILE_TOOL_NAMES:
            tool.description = _file_description(base, env)


def _detect_container() -> bool:
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    markers = ("docker", "containerd", "kubepods", "podman")
    return any(marker in cgroup.lower() for marker in markers)


def _shell_description(base: str, env: ExecutionEnvironment) -> str:
    shell_names = ", ".join(env.available_shells) if env.available_shells else "none detected"
    container = "yes" if env.in_container else "no"
    syntax_hint = _syntax_hint(env)
    return (
        f"{base}\n\n"
        "Runtime context:\n"
        f"- Platform: {env.platform_label}; container: {container}.\n"
        f"- Default cwd when working_directory is omitted: {env.default_cwd}.\n"
        f"- Process cwd: {env.process_cwd}.\n"
        f"- subprocess shell=True executable: {env.shell_executable}.\n"
        f"- Available shells: {shell_names}.\n"
        f"- Path separator: {env.path_separator!r}.\n"
        f"- {syntax_hint}\n"
        "- Cwd is not stateful between calls; use working_directory for one-off directory changes."
    )


def _file_description(base: str, env: ExecutionEnvironment) -> str:
    return (
        f"{base}\n\n"
        "Runtime path context:\n"
        f"- Relative file_path values resolve from: {env.default_cwd}.\n"
        "- Absolute paths are accepted, subject to each tool's safety checks.\n"
        "- Shell cd commands do not change file tool paths; cwd is not stateful."
    )


def _syntax_hint(env: ExecutionEnvironment) -> str:
    shells = set(env.available_shells)
    if os.name == "nt":
        if "pwsh" in shells or "powershell" in shells:
            return "Use Windows shell syntax by default; call pwsh/powershell explicitly for PowerShell-specific commands."
        return "Use Windows cmd syntax by default."
    if "bash" in shells:
        return "Use POSIX shell syntax by default; wrap Bash-specific syntax with bash -lc '...'."
    return "Use POSIX /bin/sh-compatible syntax by default."
