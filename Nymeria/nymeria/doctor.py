"""Installation diagnostics for the ``nymeria doctor`` command."""

from __future__ import annotations

import argparse
import shutil
import socket
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal

from langchain_core.messages import HumanMessage
from rich.console import Console

from .config.settings import PACKAGE_ROOT, PROJECT_ROOT, get_env_file_paths, get_settings
from .core.checkpoint_sql import sqlite_table_exists
from .core.event_bus import redact_url_credentials
from .vendor.react_agent.config import LLMConfig
from .vendor.react_agent.providers import create_llm

Status = Literal["pass", "warn", "fail"]

_REQUIRED_PYTHON = (3, 11)


@dataclass(frozen=True)
class CheckResult:
    """One rendered doctor check."""

    name: str
    status: Status
    detail: str


def run_doctor(args: argparse.Namespace) -> int:
    """Run all installation diagnostics and render a compact report."""
    console = Console()
    results = collect_checks(args)

    console.print("\n[bold]Nymeria Health Check[/bold]")
    console.print("-" * 22)
    for result in results:
        console.print(_format_result(result))

    failures = [result for result in results if result.status == "fail"]
    warnings = [result for result in results if result.status == "warn"]
    console.print()
    if failures:
        console.print(f"[red]{len(failures)} failed check(s).[/red]")
        return 1
    if warnings:
        console.print(f"[yellow]{len(warnings)} warning(s); no required checks failed.[/yellow]")
        return 0
    console.print("[green]All checks passed.[/green]")
    return 0


def collect_checks(args: argparse.Namespace) -> list[CheckResult]:
    """Collect doctor checks without rendering them."""
    project_root_arg = getattr(args, "project_root", None)
    project_root = (
        Path(project_root_arg).expanduser().resolve()
        if project_root_arg is not None
        else PROJECT_ROOT
    )
    results = [
        _check_python(),
        _check_config_files(project_root),
    ]

    try:
        if project_root_arg is not None:
            with _settings_for_project_root(project_root) as settings:
                _append_settings_checks(results, settings, args)
            return results
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001 - diagnostics should report all config failures.
        results.append(CheckResult("Settings", "fail", f"could not load settings: {_compact_error(exc)}"))
        results.append(_check_frontend())
        return results

    _append_settings_checks(results, settings, args)
    return results


def _append_settings_checks(
    results: list[CheckResult],
    settings: Any,
    args: argparse.Namespace,
) -> None:
    """Append checks that depend on loaded settings."""
    results.extend(
        [
            _check_data_dir(settings),
            _check_web_search(settings),
            _check_llm(settings, skip=bool(getattr(args, "skip_llm_test", False))),
            _check_database(settings),
            _check_redis(settings),
            _check_voice(settings),
            _check_frontend(),
            _check_port(settings),
        ]
    )


def _format_result(result: CheckResult) -> str:
    style = {
        "pass": "green",
        "warn": "yellow",
        "fail": "red",
    }[result.status]
    label = {
        "pass": "OK",
        "warn": "WARN",
        "fail": "FAIL",
    }[result.status]
    return f"[{style}][{label}][/{style}] {result.name}: {result.detail}"


def _check_python() -> CheckResult:
    version = sys.version_info
    label = f"{version.major}.{version.minor}.{version.micro}"
    if (version.major, version.minor) < _REQUIRED_PYTHON:
        return CheckResult(
            "Python",
            "fail",
            f"{label}; Nymeria requires Python {_REQUIRED_PYTHON[0]}.{_REQUIRED_PYTHON[1]}+",
        )
    return CheckResult("Python", "pass", label)


@contextmanager
def _settings_for_project_root(project_root: Path) -> Iterator[Any]:
    """
    Load settings for a specific runtime root while checks are being collected.

    ``Settings.project_root`` currently reads the module-level PROJECT_ROOT, so
    the override must remain active until all settings-backed checks finish.
    """
    from .config import settings as settings_module

    original_project_root = settings_module.PROJECT_ROOT
    settings_module.PROJECT_ROOT = project_root
    settings_module.get_settings.cache_clear()
    try:
        env_files = tuple(
            str(path) for path in settings_module.get_env_file_paths(project_root)
        )
        yield settings_module.Settings(_env_file=env_files)
    finally:
        settings_module.PROJECT_ROOT = original_project_root
        settings_module.get_settings.cache_clear()


def _check_config_files(project_root: Path) -> CheckResult:
    paths = get_env_file_paths(project_root)
    existing = [path for path in paths if path.is_file()]
    if existing:
        return CheckResult("Config", "pass", ", ".join(str(path) for path in existing))
    return CheckResult(
        "Config",
        "warn",
        f"no config file found in {project_root}; environment variables may still apply",
    )


def _check_data_dir(settings: Any) -> CheckResult:
    data_dir = Path(settings.data_dir)
    if not data_dir.exists():
        return CheckResult("Data dir", "fail", f"{data_dir} does not exist")
    if not data_dir.is_dir():
        return CheckResult("Data dir", "fail", f"{data_dir} is not a directory")

    probe = data_dir / ".doctor-write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        return CheckResult("Data dir", "fail", f"{data_dir} is not writable: {exc}")
    return CheckResult("Data dir", "pass", f"{data_dir} (writable)")


def _check_web_search(settings: Any) -> CheckResult:
    """Web capability of the default toolset: search present, fetch dependency met.

    Reads the bootstrap admin's ``default_thread_tools`` straight from
    ``profile.json`` (no UserProfileManager, which would side-effect a fresh
    install by seeding the profile); an absent or unset profile means the
    fresh-install defaults apply, whose web portion is
    ``DEFAULT_WEB_TOOL_NAMES``. Warns when the defaults carry no web search at
    all, or a link-only backend without a fetch tool (search results would be
    unreadable); the deliberate keyless ddgs default passes with upgrade
    guidance rather than warning on every fresh install.
    """
    import json

    from .core.accounts import BOOTSTRAP_USER_ID
    from .core.user_profile import DEFAULT_WEB_TOOL_NAMES

    names = None
    try:
        profile_path = (
            Path(settings.data_dir) / "users" / BOOTSTRAP_USER_ID / "profile.json"
        )
        if profile_path.exists():
            raw = json.loads(profile_path.read_text(encoding="utf-8"))
            names = (raw.get("tool_preferences") or {}).get("default_thread_tools")
    except Exception:  # noqa: BLE001 - diagnostics must not crash on a bad profile
        names = None
    if not isinstance(names, list):
        names = list(DEFAULT_WEB_TOOL_NAMES)
    search = [n for n in names if isinstance(n, str) and n.startswith("web_search_")]
    fetch = [n for n in names if isinstance(n, str) and n.startswith("fetch_url_")]
    if not search:
        return CheckResult(
            "Web search",
            "warn",
            "no web_search_* tool in the default toolset, so new threads cannot "
            "search the web; enable one in Settings -> Tools or rerun `nymeria init`",
        )
    link_only = [n for n in search if n != "web_search_perplexity"]
    if link_only and not fetch:
        return CheckResult(
            "Web search",
            "warn",
            f"{', '.join(sorted(search))} returns links only and no fetch_url_* "
            "tool is in the defaults, so results are unreadable; add "
            "fetch_url_nymeria (keyless) in Settings -> Tools",
        )
    if search == ["web_search_ddgs"]:
        return CheckResult(
            "Web search",
            "pass",
            "web_search_ddgs (keyless scraped-engine default) works with no "
            "setup; a keyed backend (Perplexity, Tavily, Brave) or a "
            "self-hosted SearXNG upgrades quality",
        )
    return CheckResult("Web search", "pass", ", ".join(sorted(search)))


def _check_llm(settings: Any, *, skip: bool) -> CheckResult:
    provider = settings.llm_provider
    model = settings.llm_model
    llm_config = _build_global_llm_config(settings)

    if not llm_config.api_key:
        return CheckResult(
            "LLM",
            "fail",
            f"{provider}/{model} has no configured API key",
        )
    if skip:
        return CheckResult("LLM", "warn", f"{provider}/{model} connection test skipped")

    try:
        _probe_llm_connection(llm_config)
    except Exception as exc:  # noqa: BLE001 - diagnostics must surface provider failures.
        return CheckResult(
            "LLM",
            "fail",
            f"{provider}/{model} connection failed: {_compact_error(exc)}",
        )
    return CheckResult("LLM", "pass", f"{provider}/{model} connected")


def _build_global_llm_config(settings: Any) -> LLMConfig:
    """Build the same deployment-level LLM config the default agent uses."""
    provider = settings.llm_provider
    base_url = settings.llm_base_url

    if provider == "anthropic":
        api_key = (
            settings.anthropic_api_key
            if base_url
            else (settings.anthropic_direct_api_key or settings.anthropic_api_key)
        )
    else:
        key_map = {
            "openai": settings.openai_api_key,
            "openrouter": settings.openrouter_api_key,
        }
        api_key = key_map.get(provider) or settings.get_api_key_for_provider()

    return LLMConfig(
        provider=provider,
        model=settings.llm_model,
        api_key=api_key,
        base_url=base_url,
        temperature=None,
        max_tokens=64,
        top_p=None,
        top_k=None,
        frequency_penalty=None,
        presence_penalty=None,
        reasoning_effort=None,
        extended_thinking=False,
        provider_route=getattr(settings, "llm_provider_route", None),
        openai_api_mode=settings.openai_api_mode,
        request_timeout=15,
        stream_max_retries=0,
    )


def _probe_llm_connection(config: LLMConfig) -> None:
    llm = create_llm(config)
    llm.invoke([HumanMessage(content="Reply with ok.")])


def _check_database(settings: Any) -> CheckResult:
    backend = settings.database_backend
    if backend == "sqlite":
        return _check_sqlite_database(settings)
    if backend == "postgres":
        return _check_postgres_database(settings)
    if backend == "memory":
        return CheckResult("Database", "warn", "memory backend configured; data will not persist")
    return CheckResult("Database", "fail", f"unsupported backend {backend!r}")


def _check_sqlite_database(settings: Any) -> CheckResult:
    checkpoint_path = Path(settings.db_path)
    accounts_path = Path(settings.data_dir) / "accounts.db"
    warnings: list[str] = []
    checkpoint_missing = False

    try:
        thread_count = _count_sqlite_threads(checkpoint_path)
    except FileNotFoundError:
        thread_count = None
        checkpoint_missing = True
    except Exception as exc:  # noqa: BLE001 - diagnostics must not crash.
        return CheckResult("Database", "fail", f"SQLite checkpoint check failed: {_compact_error(exc)}")

    try:
        user_count = _count_sqlite_users(accounts_path)
    except FileNotFoundError:
        user_count = None
        warnings.append(f"accounts DB missing at {accounts_path}")
    except Exception as exc:  # noqa: BLE001 - diagnostics must not crash.
        return CheckResult("Database", "fail", f"accounts DB check failed: {_compact_error(exc)}")

    parts = ["SQLite"]
    if thread_count is not None:
        parts.append(f"{thread_count} thread(s)")
    elif checkpoint_missing:
        parts.append(f"checkpoint DB not created yet at {checkpoint_path}")
    if user_count is not None:
        parts.append(f"{user_count} user(s)")
    parts.extend(warnings)
    return CheckResult("Database", "warn" if warnings else "pass", "; ".join(parts))


def _count_sqlite_threads(path: Path) -> int:
    if not path.is_file():
        raise FileNotFoundError(path)
    with _connect_readonly(path) as conn:
        if not sqlite_table_exists(conn, "checkpoints"):
            return 0
        row = conn.execute("SELECT COUNT(DISTINCT thread_id) AS n FROM checkpoints").fetchone()
    return int(row["n"] if row else 0)


def _count_sqlite_users(path: Path) -> int:
    if not path.is_file():
        raise FileNotFoundError(path)
    with _connect_readonly(path) as conn:
        if not sqlite_table_exists(conn, "users"):
            return 0
        row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
    return int(row["n"] if row else 0)


def _connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _check_postgres_database(settings: Any) -> CheckResult:
    if not settings.postgres_uri:
        return CheckResult("Database", "fail", "DATABASE_BACKEND=postgres but POSTGRES_URI is unset")
    try:
        import psycopg  # type: ignore[import-untyped]

        with psycopg.connect(settings.postgres_uri, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(DISTINCT thread_id) FROM checkpoints")
                row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 - diagnostics must report all failures.
        return CheckResult("Database", "fail", f"Postgres check failed: {_compact_error(exc)}")
    count = int(row[0]) if row else 0
    return CheckResult("Database", "pass", f"Postgres connected; {count} thread(s)")


def _check_redis(settings: Any) -> CheckResult:
    if not settings.redis_enabled:
        return CheckResult("Redis", "warn", "not configured (optional)")
    if not settings.redis_url:
        return CheckResult("Redis", "fail", "REDIS_ENABLED=true but REDIS_URL is unset")
    try:
        import redis

        client = redis.from_url(
            settings.redis_url,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        try:
            client.ping()
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001 - diagnostics must report all failures.
        return CheckResult("Redis", "fail", f"ping failed: {_compact_error(exc)}")
    return CheckResult("Redis", "pass", f"connected to {redact_url_credentials(settings.redis_url)}")


def _check_voice(settings: Any) -> CheckResult:
    configured = []
    failures = []

    if settings.tts_provider != "none":
        try:
            from .core.voice import get_tts_service

            get_tts_service(settings)
            # Providers import their engine lazily on first synthesis, so the
            # factory succeeds even without the package; check importability here.
            if settings.tts_provider == "kokoro" and not settings.tts_base_url:
                from .core.voice_local import INSTALL_HINT, local_tts_importable

                if not local_tts_importable():
                    raise RuntimeError(f"kokoro-onnx is not installed. {INSTALL_HINT}")
            if settings.tts_provider == "edge":
                try:
                    import edge_tts  # noqa: F401  # pyrefly: ignore[missing-import]
                except ImportError:
                    raise RuntimeError("the edge-tts package is not installed (pip install edge-tts)")
            if settings.tts_provider == "gemini":
                try:
                    from google import genai  # noqa: F401
                except ImportError:
                    raise RuntimeError("the google-genai package is not installed (pip install google-genai)")
            configured.append(f"TTS={settings.tts_provider}")
        except Exception as exc:  # noqa: BLE001 - diagnostics must report all failures.
            failures.append(f"TTS={settings.tts_provider}: {_compact_error(exc)}")

    if settings.stt_provider != "none":
        try:
            from .core.voice import get_stt_service

            get_stt_service(settings)
            if settings.stt_provider == "faster-whisper" and not settings.stt_base_url:
                from .core.voice_local import INSTALL_HINT, local_stt_importable

                if not local_stt_importable():
                    raise RuntimeError(f"faster-whisper is not installed. {INSTALL_HINT}")
            configured.append(f"STT={settings.stt_provider}")
        except Exception as exc:  # noqa: BLE001 - diagnostics must report all failures.
            failures.append(f"STT={settings.stt_provider}: {_compact_error(exc)}")

    if failures:
        return CheckResult("Voice", "fail", "; ".join(failures))
    if configured:
        # pydub shells out to the ffmpeg system binary for audio conversion.
        # pip/uv cannot install it, so flag its absence when voice is in use.
        if shutil.which("ffmpeg") is None:
            return CheckResult(
                "Voice",
                "warn",
                f"{', '.join(configured)} configured, but ffmpeg was not found on "
                "PATH. Audio conversion needs the ffmpeg system package "
                "(e.g. apt install ffmpeg / brew install ffmpeg).",
            )
        return CheckResult("Voice", "pass", ", ".join(configured))
    return CheckResult("Voice", "warn", "not configured (optional)")


def _check_frontend() -> CheckResult:
    for path in _frontend_candidates():
        index_path = path / "index.html"
        if index_path.is_file():
            return CheckResult("Frontend", "pass", f"bundled at {path}")
    return CheckResult("Frontend", "warn", "bundled frontend not found")


def _frontend_candidates() -> tuple[Path, ...]:
    package_frontend = PACKAGE_ROOT / "frontend"
    source_frontend = PACKAGE_ROOT.parent / "frontend"
    return (package_frontend, source_frontend)


def _check_port(settings: Any) -> CheckResult:
    host = _connectable_host(settings.api_host)
    port = int(settings.api_port)
    if _port_in_use(host, port):
        return CheckResult("Port", "warn", f"{host}:{port} is already in use")
    return CheckResult("Port", "pass", f"{host}:{port} is available")


def _connectable_host(host: str) -> str:
    if host in {"0.0.0.0", "::", ""}:
        return "127.0.0.1"
    return host


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) == 0


def _compact_error(exc: BaseException) -> str:
    message = str(exc).strip().replace("\n", " ")
    return message[:300] if message else exc.__class__.__name__
