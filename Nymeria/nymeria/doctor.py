"""Installation diagnostics for the ``nymeria doctor`` command."""

from __future__ import annotations

import argparse
import os
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
            _check_secrets_key(project_root=_project_root_override(args)),
            _check_web_search(settings, project_root=_project_root_override(args)),
        ]
    )
    searxng = _check_searxng(settings, project_root=_project_root_override(args))
    if searxng is not None:
        results.append(searxng)
    results.extend(
        [
            _check_llm(settings, skip=bool(getattr(args, "skip_llm_test", False))),
            _check_database(settings),
            _check_redis(settings),
            _check_voice(settings),
            _check_frontend(),
            _check_port(settings),
        ]
    )
    override = _project_root_override(args)
    project_root = override if override is not None else PROJECT_ROOT
    # Only for this install's own root: the extra is a property of THIS
    # interpreter, and the one caller that names another root (the wizard's
    # final doctor) may be configuring a Docker image, whose Python is not this
    # one. The wizard reports the extra itself for the installs it serves.
    local_rag = _check_local_rag(settings) if override is None else None
    if local_rag is not None:
        results.append(local_rag)
    browser = _check_server_browser(settings, project_root)
    if browser is not None:
        results.append(browser)
    settings_file = _check_settings_file(project_root, own_root=override is None)
    if settings_file is not None:
        results.append(settings_file)


def _project_root_override(args: argparse.Namespace) -> Path | None:
    project_root_arg = getattr(args, "project_root", None)
    if project_root_arg is None:
        return None
    return Path(project_root_arg).expanduser().resolve()


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


def _check_secrets_key(project_root: Path | None = None) -> CheckResult:
    """The credential vault key (#101 entry 17 of 2026-08-24).

    The process environment wins, as it does for every setting (``run.py``
    loaded this install's env files into it before doctor runs); for another
    root, that root's own env files fill in a key the environment lacks.
    Missing or malformed is a warning, not a failure: the install runs, and
    only credential saves and reads break.
    """
    from .core.secrets import (
        SECRETS_KEY_ENV_VAR,
        SECRETS_KEY_MINT_COMMAND,
        secrets_key_problem,
    )

    raw = os.environ.get(SECRETS_KEY_ENV_VAR) or ""
    if not raw and project_root is not None:
        from dotenv import dotenv_values

        for path in get_env_file_paths(project_root):
            try:
                if path.is_file():
                    raw = dotenv_values(path).get(SECRETS_KEY_ENV_VAR) or raw
            except (OSError, UnicodeDecodeError, ValueError):
                continue
    problem = secrets_key_problem(raw)
    if problem is None:
        return CheckResult("Secrets key", "pass", f"{SECRETS_KEY_ENV_VAR} is set")
    if problem == "invalid":
        return CheckResult(
            "Secrets key",
            "warn",
            f"{SECRETS_KEY_ENV_VAR} is set but is not a valid key, so saving or "
            "reading any credential will fail; restore the original value if it "
            f"was changed by mistake, otherwise replace it with `{SECRETS_KEY_MINT_COMMAND}`",
        )
    return CheckResult(
        "Secrets key",
        "warn",
        f"{SECRETS_KEY_ENV_VAR} is not set, so saving any credential will fail; "
        f"generate one with `{SECRETS_KEY_MINT_COMMAND}` and add it to your env file",
    )


def _check_local_rag(settings: Any) -> CheckResult | None:
    """A local embedder or reranker needs the local-rag extra (#101 entry 21).

    ``None`` when nothing asks for a local model, so installs on a hosted
    embedder get no row at all.
    """
    from .config.settings import (
        describe_missing_local_rag,
        local_rag_keys,
        local_rag_keys_without_extra,
    )

    embedding = getattr(settings, "embedding_provider", None)
    rerank = getattr(settings, "rag_rerank_provider", None)
    rerank_enabled = bool(getattr(settings, "rag_rerank_enabled", False))
    local = local_rag_keys(embedding, rerank, rerank_enabled)
    if not local:
        return None
    missing = local_rag_keys_without_extra(embedding, rerank, rerank_enabled)
    if not missing:
        return CheckResult("Local RAG", "pass", f"{', '.join(local)}=local; extra installed")
    return CheckResult(
        "Local RAG",
        "warn",
        f"{describe_missing_local_rag(missing)}; install nymeriaos[local-rag] "
        "(Docker: NYMERIA_LOCAL_RAG=1, then up -d --build) or choose a hosted "
        "provider with `nymeria init`",
    )


def _init_seed_tools(project_root: Path | None) -> tuple[list[str], str] | None:
    """The init-seed carrier's tool names and where they were read, else None.

    ``NYMERIA_INIT_DEFAULT_THREAD_TOOLS`` is what the bootstrap admin's profile
    is seeded with when it is first created; on a Docker host that profile lives
    in the container's volume and the wizard's picks ride in the env file. The
    process environment wins (``run.py`` loaded this install's env files into
    it); for another root, that root's own env files fill in, a later file over
    an earlier one, as for the secrets key. The second element names the source
    for a row's detail. Callers consult this only while the bootstrap profile
    does not exist: once it does, the carrier is spent.
    """
    from .config.init_seed_env import INIT_DEFAULT_THREAD_TOOLS_ENV, parse_init_name_list

    names = parse_init_name_list(os.environ.get(INIT_DEFAULT_THREAD_TOOLS_ENV))
    if names:
        return names, f"{INIT_DEFAULT_THREAD_TOOLS_ENV} in the environment"
    if project_root is None:
        return None
    from dotenv import dotenv_values

    found: tuple[list[str], str] | None = None
    for env_path in get_env_file_paths(project_root):
        try:
            if not env_path.is_file():
                continue
            names = parse_init_name_list(dotenv_values(env_path).get(INIT_DEFAULT_THREAD_TOOLS_ENV))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if names:
            found = (names, f"{INIT_DEFAULT_THREAD_TOOLS_ENV} in {env_path}")
    return found


def _bootstrap_profile_path(settings: Any) -> Path:
    from .core.accounts import BOOTSTRAP_USER_ID

    return Path(settings.data_dir) / "users" / BOOTSTRAP_USER_ID / "profile.json"


def _check_web_search(settings: Any, *, project_root: Path | None = None) -> CheckResult:
    """Web capability of the default toolset: search present, fetch dependency met.

    Reads the bootstrap admin's ``default_thread_tools`` straight from
    ``profile.json`` (no UserProfileManager, which would side-effect a fresh
    install by seeding the profile). While that profile does not exist, the
    init-seed carrier it will be seeded from (:func:`_init_seed_tools`, the
    Docker host's case) is read instead, and the detail names where; with
    neither, or a profile without the field, the fresh-install defaults apply,
    whose web portion is ``DEFAULT_WEB_TOOL_NAMES``. Warns when the defaults
    carry no web search at all, or a link-only backend without a fetch tool
    (search results would be unreadable); the deliberate keyless ddgs default
    passes with upgrade guidance rather than warning on every fresh install.
    """
    import json

    from .core.user_profile import DEFAULT_WEB_TOOL_NAMES

    names = None
    source = None
    try:
        profile_path = _bootstrap_profile_path(settings)
        if profile_path.exists():
            raw = json.loads(profile_path.read_bytes())
            names = (raw.get("tool_preferences") or {}).get("default_thread_tools")
        else:
            seeded = _init_seed_tools(project_root)
            if seeded is not None:
                names, source = seeded
    except Exception:  # noqa: BLE001 - diagnostics must not crash on a bad profile
        names = None
        source = None
    if not isinstance(names, list):
        names = list(DEFAULT_WEB_TOOL_NAMES)
        source = None
    row = _web_search_row(names)
    if source is not None:
        row = CheckResult(
            row.name,
            row.status,
            f"{row.detail} (list from {source}, which seeds the admin's profile on first start)",
        )
    return row


def _web_search_row(names: list[Any]) -> CheckResult:
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


_SEARXNG_TOOL = "web_search_searxng"


def _searxng_in_default_tools(settings: Any, project_root: Path | None) -> bool:
    """Whether any account's default toolset carries ``web_search_searxng``.

    Raw reads of every ``data/users/*/profile.json`` (never UserProfileManager,
    which seeds a missing profile as a side effect), so a non-admin account
    carrying SearXNG counts too. When the bootstrap admin's profile does not
    exist yet, the init-seed carrier (:func:`_init_seed_tools`, the same reader
    the ``Web search`` row uses) counts as well. With neither, the fresh
    defaults apply, and they carry no SearXNG. A per-thread enable is not
    covered here; the tool's own ``[Error]`` covers it at run time.
    """
    import json

    data_dir = getattr(settings, "data_dir", None)
    users_dir = Path(data_dir) / "users" if data_dir else None
    profiles: list[Path] = []
    if users_dir is not None:
        try:
            profiles = sorted(users_dir.glob("*/profile.json"))
        except OSError:
            profiles = []
    for path in profiles:
        try:
            raw = json.loads(path.read_bytes())
            names = (raw.get("tool_preferences") or {}).get("default_thread_tools")
        except Exception:  # noqa: BLE001 - a bad profile is another check's finding
            continue
        if isinstance(names, list) and _SEARXNG_TOOL in names:
            return True
    if users_dir is not None and _bootstrap_profile_path(settings).exists():
        return False
    seeded = _init_seed_tools(project_root)
    return seeded is not None and _SEARXNG_TOOL in seeded[0]


def _check_searxng(settings: Any, *, project_root: Path | None = None) -> CheckResult | None:
    """The SearXNG backend, when a default toolset uses it (#296).

    ``None`` unless :func:`_searxng_in_default_tools`: the compose files set
    ``SEARXNG_BASE_URL`` on every full stack, so the URL alone says nothing
    about use. One canary search (``core/searxng_health.probe_searxng``)
    against the OPERATOR's ``SEARXNG_BASE_URL`` only; a user's saved address
    is never read. A search, not ``/healthz``: an instance whose every engine
    is blocked answers healthz OK (measured), which is the incident this row
    exists for. A Docker service host seen from outside a container is not
    probed (it does not resolve here) and the row says so rather than passing.
    WARN at most, never FAIL: search is optional, and a FAIL in the wizard's
    final doctor would skip start-now.
    """
    try:
        if not _searxng_in_default_tools(settings, project_root):
            return None
        return _searxng_row(settings)
    except Exception as exc:  # noqa: BLE001 - diagnostics report, never raise or hide
        return CheckResult("SearXNG", "warn", f"check failed: {_compact_error(exc)}")


def _searxng_row(settings: Any) -> CheckResult:
    from .config.settings import _docker_service_host, _in_container
    from .core.searxng_health import (
        PROBE_TIMEOUT_SECONDS,
        base_url_problem,
        describe_engines,
        probe_searxng,
    )

    base_url = str(getattr(settings, "searxng_base_url", None) or "").strip()
    if not base_url:
        return CheckResult(
            "SearXNG",
            "warn",
            f"{_SEARXNG_TOOL} is in the default tools but SEARXNG_BASE_URL is not "
            "set, so it fails unless a user saved their own SearXNG address (not "
            "checked here); set SEARXNG_BASE_URL or rerun `nymeria init`",
        )
    if base_url_problem(base_url) is not None:
        # The rule `nymeria init` applies to the same setting. Never echoed
        # and never probed: a malformed or scheme-less value ("user:pw@host:1",
        # "searxng:8080") can still carry credentials, and urlsplit finds no
        # userinfo in it to redact.
        return CheckResult(
            "SearXNG",
            "warn",
            "SEARXNG_BASE_URL is not a valid http:// or https:// URL, so "
            "web_search_searxng cannot use it; fix it or rerun `nymeria init`",
        )
    service_host = _docker_service_host(base_url)
    if service_host and not _in_container():
        return CheckResult(
            "SearXNG",
            "warn",
            f"not checked from the host: SEARXNG_BASE_URL names the Docker service "
            f"host '{service_host}', which resolves only inside the stack's network. "
            "Check it there: `docker exec <api container> python run.py doctor`",
        )

    shown = redact_url_credentials(base_url)
    probe = probe_searxng(base_url)
    if probe.outcome == "unreachable":
        detail = (
            f"cannot reach {shown} ({probe.reason}); start the sidecar (Docker: "
            "`docker compose --profile search up -d searxng`) or fix SEARXNG_BASE_URL"
        )
    elif probe.outcome == "timeout":
        detail = (
            f"{shown} did not answer a test search within "
            f"{PROBE_TIMEOUT_SECONDS:g}s ({probe.reason})"
        )
    elif probe.outcome == "http_error" and probe.status_code == 403:
        detail = (
            f"{shown} refused a JSON test search (HTTP 403); the instance's "
            "search.formats setting must include json"
        )
    elif probe.outcome == "http_error" and probe.status_code == 429:
        detail = (
            f"{shown} rate-limited the test search (HTTP 429); a private instance "
            "should set server.limiter: false"
        )
    elif probe.outcome == "http_error":
        detail = f"{shown} answered the test search with HTTP {probe.status_code}"
    elif probe.outcome == "not_json":
        detail = f"{shown} answered without JSON; is SEARXNG_BASE_URL a SearXNG instance?"
    elif probe.result_count:
        detail = f"{shown} answered a test search with {probe.result_count} result(s)"
        if probe.failed_engines:
            detail += (
                f"; {len(probe.failed_engines)} engine(s) failed: "
                f"{describe_engines(probe.failed_engines)}"
            )
        return CheckResult("SearXNG", "pass", detail)
    elif probe.failed_engines:
        detail = (
            f"{shown} is up, but its engines failed a test search with no results: "
            f"{describe_engines(probe.failed_engines)}. Upstream engines are refusing "
            "or unreachable from the instance (common on datacenter IPs): try a newer "
            "SearXNG image or other engines, or make web_search_ddgs the default "
            "(Settings -> Tools)"
        )
    else:
        detail = (
            f"{shown} answered a test search with no results and no engine errors; "
            "check the instance's enabled engines"
        )
    return CheckResult("SearXNG", "warn", detail)


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


def _check_server_browser(settings: Any, project_root: Path) -> CheckResult | None:
    """The server browser (headless Chrome + extension), when this install has one.

    None when nothing was ever configured, so a popup-only install shows no
    row. Warn, never fail: the backend runs fine without it, and the detail
    carries the fix (`nymeria browser status` names each problem). A rig that
    had to fall back to `--no-sandbox` at configure time warns for as long as
    it runs that way, with the durable fix named, because the opt-out was
    measured rather than chosen.
    """
    from . import server_browser as sb

    home_value = getattr(settings, "server_browser_home", None) or os.environ.get(sb.HOME_ENV_KEY)
    home = sb.resolve_rig_home(project_root, home_value or None)
    if not home_value and not home.rig_json.exists():
        return None
    if home_value and not home.path.exists():
        # The key is a PRESENCE signal, and on the Docker stack it is set in
        # the api and worker containers on purpose while the rig itself runs
        # on the HOST: the path is meaningless in here. Reporting "not
        # installed" from inside a container would accuse a healthy rig, so
        # say what is actually known, which is that nothing is at that path
        # on this machine.
        return CheckResult(
            "Server browser",
            "warn",
            f"no rig at {home.path} on this machine. On the Docker stack the "
            "rig runs on the host, so check it there with `nymeria browser "
            "status`; otherwise `nymeria browser install` provisions one.",
        )
    try:
        report = sb.status(home)
    except Exception as exc:  # noqa: BLE001 - diagnostics report, never raise
        return CheckResult("Server browser", "warn", f"status failed: {_compact_error(exc)}")
    if report.backend_reachable is False:
        # `nymeria init --doctor` runs BEFORE start-now launches the backend, so
        # on a healthy fresh install the rig's backend probes fail for the one
        # reason that is not the rig's fault. Report what is locally true and
        # say why the rest is unknown, rather than accusing a browser that is
        # running exactly as installed.
        local = "installed and running" if report.running else "installed"
        if not report.installed:
            local = "not installed"
        return CheckResult(
            "Server browser",
            "warn",
            f"{local}; the backend is not up, so whether it is connected is "
            "unknown. Re-check with `nymeria browser status` once Nymeria is "
            "running.",
        )
    if report.healthy:
        summary = (
            f"{report.chrome_version or 'Chrome'} connected"
            + (f" as {report.identity}" if report.identity else "")
            # The kind is a constant here, not a lookup: doctor only ever
            # describes THIS install's rig, and a rig is a server browser by
            # construction. It is named anyway because the refusals and the
            # roster both speak in kinds, so the row should too.
            + f" ({report.label or 'server browser'}, a server browser, "
            f"debug port {report.debug_port})"
        )
        if report.no_sandbox:
            return CheckResult(
                "Server browser",
                "warn",
                summary
                + "; running with --no-sandbox (user namespaces are restricted here; "
                "an AppArmor profile for the Chrome binary is the durable fix, then "
                "`nymeria browser configure --sandbox on`)",
            )
        return CheckResult("Server browser", "pass", summary)
    problems = "; ".join(report.problems) or "not healthy"
    return CheckResult("Server browser", "warn", f"{problems} (details: nymeria browser status)")


def _check_settings_file(project_root: Path, *, own_root: bool) -> CheckResult | None:
    """What the app's saved settings file overrides, on the container shapes (#434).

    That file (``NYMERIA_SETTINGS_FILE``, ``/data/settings.env``) loads last,
    so a value saved in the app silently beats the one ``.env.docker`` sets.
    In a process that loads it (doctor run inside the API container): WARN
    when a credential or route key is overridden (the shape that sends a key
    to the wrong place), PASS naming any other override and the keys saved
    only in the app. On a host whose root holds ``.env.docker`` the file sits
    in the data volume and is invisible from here, so a PASS row points at the
    two places that can see it rather than guessing. Anything else: no row.
    Names only, never values.
    """
    from .config.settings import (
        SETTINGS_CLEAR_REMEDY,
        direct_slot_hints,
        runtime_settings_file,
        runtime_settings_shadows,
        shadow_labels,
    )

    runtime = runtime_settings_file() if own_root else None
    if runtime is None:
        if not (project_root / ".env.docker").is_file():
            return None
        return CheckResult(
            "Settings file",
            "pass",
            "settings saved in the app live in the data volume (/data/settings.env), "
            "which loads last and overrides .env.docker but is not visible from "
            "here. To see what it overrides, run /status as an admin, or doctor "
            "inside the API container (`docker exec <api container> python run.py "
            "doctor`).",
        )
    shadows = runtime_settings_shadows()
    overriding = [shadow for shadow in shadows if shadow.shadows]
    app_only = [shadow for shadow in shadows if not shadow.shadows]
    detail = f"{runtime} (saved in the app, loads last)"
    if overriding:
        detail += (
            f"; overrides values set elsewhere: {shadow_labels(overriding)}. To use "
            f"the other value, run {SETTINGS_CLEAR_REMEDY}"
        )
    else:
        detail += "; overrides nothing set elsewhere"
    if app_only:
        detail += f"; saved only in the app: {shadow_labels(app_only)}"
    hints = direct_slot_hints(overriding)
    if hints:
        detail += ". " + " ".join(hints)
    severe = any(shadow.severe for shadow in overriding)
    return CheckResult("Settings file", "warn" if severe else "pass", detail)


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
