#!/usr/bin/env python
"""
Nymeria - Entry point script.

Usage:
    python run.py cli          # Start CLI interface
    python run.py api          # Start REST API server
    python run.py api --port 8080  # Start API on custom port
    python run.py slim         # Start single-process local launcher (API + ticker + MCP)
    python run.py doctor           # Diagnose local configuration
    python run.py worker           # Start worker (ticker only, for Docker)
    python run.py discord-bot     # Start Discord bot (gateway mode)
    python run.py telegram-bot    # Start Telegram bot (polling mode)
    python run.py slack-bot       # Start Slack bot (Socket Mode)
    python run.py mcp              # Start MCP server (stdio mode)
    python run.py mcp --http       # Start MCP server (HTTP mode)
    python run.py mcp --port 8001  # MCP HTTP mode on custom port
    python run.py service run      # Run gateway in foreground (debug)
    python run.py service install  # Install the background service (systemd user unit / launchd agent)
    python run.py service status   # Background-service state + health probe
"""

import argparse
import logging
import signal
import sys
import warnings
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, NamedTuple, Optional

import os

from nymeria._runtime_paths import default_user_project_root

# Add project to path — for PyInstaller frozen builds and installed package
# entrypoints, the bundled modules are already on sys.path, but we still need
# the runtime project root for environment/config resolution.
if os.environ.get("NYMERIA_PROJECT_ROOT"):
    _project_root = Path(os.environ["NYMERIA_PROJECT_ROOT"]).expanduser().resolve()
elif getattr(sys, "frozen", False):
    _project_root = default_user_project_root()
else:
    _project_root = Path(__file__).resolve().parent
sys.path.insert(0, str(_project_root))

from dotenv import load_dotenv


def _load_environment() -> None:
    """Load environment files relative to project root, overriding inherited values."""
    project_root = _project_root

    # Load base config first, then package-user config and docker overrides if
    # present.
    # override=True ensures restarts pick up latest .env values even when
    # the parent process has stale exported environment variables.
    for filename in (".env", "config.env", ".env.docker"):
        try:
            load_dotenv(project_root / filename, override=True)
        except UnicodeDecodeError:
            pass


# Load environment variables before importing settings/users of os.environ
_load_environment()


_SERVICE_TOKEN_REQUIRED_COMMANDS = {
    # NOTE: "worker" is intentionally absent. The worker depends only on
    # postgres/redis (not api-healthy) and can start before the api self-mints
    # the service token onto the shared volume, so it must NOT be gated by the
    # early dispatch check; ``run_worker`` resolves the token after its own API
    # health wait instead. (The cosmetic "not set" warning is suppressed for it
    # in ``main`` so this omission stays silent.)
    "discord-bot": "the Discord bot",
    "telegram-bot": "the Telegram bot",
    "slack-bot": "the Slack bot",
    "mcp": "the MCP thin client",
}

_LANGGRAPH_ALLOWED_OBJECTS_WARNING = (
    r"The default value of `allowed_objects` will change in a future version\."
)


class _StoreUserIdAction(argparse.Action):
    """Track whether --user-id was explicitly supplied."""

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        setattr(namespace, "user_id_explicit", True)


def _suppress_runtime_dependency_warnings() -> None:
    """Hide known upstream warnings that otherwise appear before CLI output."""
    try:
        from langchain_core._api.deprecation import LangChainPendingDeprecationWarning
    except Exception:  # noqa: BLE001 - fallback keeps the launcher robust.
        warning_category = Warning
    else:
        warning_category = LangChainPendingDeprecationWarning

    warnings.filterwarnings(
        "ignore",
        message=_LANGGRAPH_ALLOWED_OBJECTS_WARNING,
        category=warning_category,
    )


def _redis_url_for_display(redis_url: str) -> str:
    """Redact Redis credentials before writing startup output."""
    from nymeria.core.event_bus import redact_url_credentials

    return redact_url_credentials(redis_url)


def _stdin_is_interactive() -> bool:
    """True when we can prompt the user (both stdin and stdout are a TTY)."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


_BOT_API_URL_HELP = "URL of running Nymeria API (e.g. http://localhost:8000)"


def _resolve_api_url(args: argparse.Namespace) -> str:
    """Thin-client API URL: explicit ``--api-url``, else the Docker default.

    Shared by the chat-platform bot runners. The worker resolves its own URL
    (it also honours the ``NYMERIA_API_URL`` env var) and the MCP server
    defers resolution, so neither routes through this helper.
    """
    return getattr(args, "api_url", None) or "http://nymeria-api:8000"


def _install_exit_handlers(
    message: str,
    *,
    on_stop: Optional[Callable[[], None]] = None,
    hard_exit: bool = True,
) -> None:
    """Register SIGINT/SIGTERM handlers shared by the long-running runners.

    The handler prints *message*, runs *on_stop* if given, then (by default)
    calls ``os._exit(0)``. Hard exit is required for runners that spawn
    non-daemon threads (e.g. the worker's ThreadPoolExecutor) which would
    otherwise keep the process alive after a graceful stop; pass
    ``hard_exit=False`` for runners that unwind their own main loop.
    """

    def signal_handler(signum, frame):  # noqa: ANN001 - signal handler signature
        print(message)
        if on_stop is not None:
            on_stop()
        if hard_exit:
            os._exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


def _add_api_url_arg(
    parser: argparse.ArgumentParser, *, help_text: str = _BOT_API_URL_HELP
) -> None:
    """Register the shared ``--api-url`` thin-client argument on a subparser."""
    parser.add_argument("--api-url", default=None, help=help_text)


def validate_config(skip_api_key: bool = False, suppress_service_token_warning: bool = False) -> None:
    """
    Validate configuration before starting any command.

    Args:
        skip_api_key: If True, skip NYMERIA_API_KEY check (for service status checks)
        suppress_service_token_warning: If True, omit the generic service-token
            warning because launch-mode validation will enforce it as fatal.

    Exits with code 1 if critical errors are found.
    """
    from nymeria.config import get_settings

    settings = get_settings()
    # Locals are named config_* so they do not shadow the stdlib ``warnings``
    # module imported at the top of this file.
    config_errors, config_warnings = settings.validate_runtime()

    # Filter out API key error if skip_api_key is True
    if skip_api_key:
        config_errors = [e for e in config_errors if "NYMERIA_API_KEY" not in e]
    if suppress_service_token_warning:
        config_warnings = [w for w in config_warnings if "NYMERIA_SERVICE_TOKEN not set" not in w]

    # Print warnings (non-fatal)
    if config_warnings:
        print("\n[Configuration Warnings]")
        print("-" * 50)
        for warning in config_warnings:
            print(f"  [!] {warning}")
        print()

    # Print errors and exit if any critical issues
    if config_errors:
        print("\n[Configuration Error]")
        print("-" * 50)
        print("NymeriaOS cannot start due to missing configuration:\n")
        for error in config_errors:
            print(f"  [X] {error}\n")
        print("-" * 50)
        print("\nQuick Setup - run the guided wizard. It writes your config, creates")
        print("the data directory, and mints an admin sign-in token:")
        print("\n      nymeria init            (installed via pip/uv)")
        print("      python3 run.py init     (from a source checkout)")
        print("\nAdvanced: set the keys manually in .env (or copy .env.docker.example")
        print("to .env.docker for the Docker stack), then re-run.")
        print("\nSee docs/QUICKSTART.md for detailed instructions.")

        # First-run bridge: in an interactive terminal, offer to launch the
        # wizard now instead of dead-ending here. Non-interactive contexts
        # (Docker, pipes, CI) just see the guidance above and exit.
        if _stdin_is_interactive():
            try:
                answer = input("\nRun 'nymeria init' now? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "n"
            if answer in ("", "y", "yes"):
                init_args = build_parser().parse_args(["init"])
                sys.exit(run_init(init_args) or 0)

        sys.exit(1)


def _service_token_requirement(args: argparse.Namespace) -> str | None:
    """Return the human-readable role requiring NYMERIA_SERVICE_TOKEN, if any."""
    command = getattr(args, "command", None)
    if command == "service":
        # Only the foreground gateway run needs the token; the service
        # manager actions (install/uninstall/status/restart) do not talk
        # to the API as a privileged client. A missing/None action means
        # the bare `nymeria service` invocation, which runs the gateway.
        if (getattr(args, "action", None) or "run") == "run":
            return "the foreground gateway service"
        return None
    if command is None:
        return None
    return _SERVICE_TOKEN_REQUIRED_COMMANDS.get(command)


def _require_service_token(settings, role: str, *, stream=None) -> str:
    """Return the admin service token or fail with provisioning guidance.

    Falls back to the token the api self-mints onto the shared data volume
    (``data/SLIM_SERVICE_TOKEN.txt``) when ``NYMERIA_SERVICE_TOKEN`` is unset, so
    thin clients that start after the api is healthy (mcp, bots, and
    the worker once it has waited for the API) pick it up with no operator
    provisioning.
    """
    from nymeria.core.service_bootstrap import read_service_token_file

    token = settings.nymeria_service_token
    if isinstance(token, str):
        token = token.strip()
    if not token:
        token = read_service_token_file(getattr(settings, "data_dir", None))
    if token:
        return token

    stream = stream or sys.stdout
    print(f"\n[Error] NYMERIA_SERVICE_TOKEN is required for {role}.", file=stream)
    print("  Provision the bot-service admin once:", file=stream)
    print("    docker exec nymeria-api python run.py users add bot-service@localhost \\", file=stream)
    print("        --role admin --id bot-service", file=stream)
    print("  Then put the printed token into NYMERIA_SERVICE_TOKEN in .env.docker.", file=stream)
    sys.exit(1)


def _service_api_client(api_url: str, api_key: str, settings):
    """Build a thin-client API client with self-mint token refresh wired.

    The refresher lets a long-running thin client (worker, bots)
    pick up an api re-mint of the shared service token on a 401 without a
    process restart. An operator-pinned NYMERIA_SERVICE_TOKEN makes the
    refresh a no-op, so this is safe for every launch path.
    """
    from nymeria.core.service_bootstrap import service_token_refresher
    from nymeria.triggers.api_client import NymeriaAPIClient

    return NymeriaAPIClient(
        base_url=api_url,
        api_key=api_key,
        token_refresher=service_token_refresher(settings),
    )


def _require_bot_sdk(module, platform: str, extra: str) -> None:
    """Fail with install guidance if a bot's optional SDK is not installed.

    Chat-platform SDKs live in per-platform extras (nymeriaos[discord], etc.) and
    are absent from a default/slim install. Each bot module exposes
    ``SDK_AVAILABLE``; when it is False we print an actionable message instead of
    letting the bot crash with a raw ImportError/AttributeError on instantiation.
    """
    if not getattr(module, "SDK_AVAILABLE", True):
        print(f"\n[Error] {platform} support is not installed.")
        print(f"  Install it with:  pip install 'nymeriaos[{extra}]'")
        print("  (or 'nymeriaos[bots]' to install every chat platform at once)")
        sys.exit(1)


def _require_launch_mode_service_token(args: argparse.Namespace, settings) -> None:
    """Fail early for launch modes that make trusted internal API calls."""
    role = _service_token_requirement(args)
    if not role:
        return
    stream = sys.stderr if getattr(args, "command", None) == "mcp" else sys.stdout
    _require_service_token(settings, role, stream=stream)


def setup_logging(level: str = "INFO", file_mode: bool = False) -> None:
    """
    Configure logging for the application.

    Uses the centralized logging_config module which provides:
    - Named debug profiles (LOG_PROFILES env var)
    - Per-module overrides (LOG_MODULES env var)
    - Compact NymeriaFormatter with short module names and ANSI color
    - Third-party noise suppression (httpx, langchain, etc.)
    - Automatic log file with rotation alongside console output

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR)
        file_mode: If True, ONLY log to file (no console output)
    """
    from nymeria.config import get_settings
    from nymeria.config.logging_config import (
        configure_logging,
        parse_profiles_from_env,
        parse_module_overrides_from_env,
    )

    settings = get_settings()

    # Build a rotating file handler for persistent logs.
    # Always enabled so logs are readable from other terminals (e.g. Claude Code
    # on WSL while the API runs in PowerShell on Windows).
    log_dir = settings.logs_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / settings.service_log_file

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=settings.service_log_max_bytes,
        backupCount=settings.service_log_backup_count,
        encoding="utf-8",
    )

    configure_logging(
        base_level=level,
        profiles=parse_profiles_from_env(),
        module_overrides=parse_module_overrides_from_env(),
        file_handler=file_handler,
        use_color=not file_mode,  # No ANSI in file-only mode
    )


def build_cli_runtime_config(args: argparse.Namespace):
    """Build the CLI runtime config from parsed launch flags."""
    from nymeria.triggers.cli.app import CLIRuntimeConfig

    positional_ref = " ".join(getattr(args, "thread_ref", []) or []).strip() or None
    startup_thread_ref = args.thread or positional_ref
    rich_scroll_region_arg = getattr(args, "rich_scroll_region", None)
    rich_scroll_region_env = os.environ.get("NYMERIA_CLI_RICH_SCROLL_REGION")
    if rich_scroll_region_arg is None:
        rich_scroll_region = (
            _truthy_env(rich_scroll_region_env)
            if rich_scroll_region_env is not None
            else True
        )
    else:
        rich_scroll_region = bool(rich_scroll_region_arg)

    # Emit deprecation warnings for old thread-selection paths
    is_list = positional_ref and positional_ref.casefold() == "list"
    if positional_ref and not is_list:
        print(
            f"Warning: positional thread ref is deprecated. "
            f"Use: nymeria cli -r {positional_ref!r}",
            file=sys.stderr,
        )
    if args.thread:
        print(
            f"Warning: --thread/-t is deprecated. "
            f"Use: nymeria cli -r {args.thread!r}",
            file=sys.stderr,
        )

    return CLIRuntimeConfig(
        # --transport defaults to None at the argparse layer (so its mutually
        # exclusive group treats an explicit value as a real selection); resolve
        # the thin-client default here, the single place transport is consumed.
        transport=args.transport or "api",
        renderer=args.renderer,
        api_url=args.api_url,
        api_key=args.api_key,
        user_id=args.user_id,
        user_id_explicit=bool(getattr(args, "user_id_explicit", False)),
        animation=args.animation,
        ascii_only=args.ascii_only,
        color=args.color,
        rich_scroll_region=rich_scroll_region,
        startup_thread_ref=startup_thread_ref,
        list_threads_on_startup=(
            args.thread is None and bool(positional_ref) and positional_ref.casefold() == "list"
        ),
        continue_last=bool(getattr(args, "continue_last", False)),
        resume_ref=getattr(args, "resume_ref", None),
        oneshot_message=getattr(args, "message", None),
        oneshot_format=getattr(args, "output_format", "plain") or "plain",
    )


def _truthy_env(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def run_cli(args: argparse.Namespace) -> None:
    """Run the CLI interface."""
    # Handle --export as a standalone non-interactive operation
    if getattr(args, "export", None):
        logging.getLogger("nymeria").setLevel(logging.CRITICAL)
        sys.exit(_run_export(args))

    # Suppress logging for clean CLI experience — errors like missing API keys
    # (e.g. RAG embedding) are expected in local dev and shouldn't clutter the REPL.
    # Fatal issues still surface via stream error events rendered by the CLI.
    logging.getLogger("nymeria").setLevel(logging.CRITICAL)

    from nymeria.triggers.cli import run_cli as start_cli

    runtime_config = build_cli_runtime_config(args)
    agent = None
    if runtime_config.transport == "local":
        # Pin the embedded agent to local SQLite (Redis off) unless the user
        # opted to share the configured backend. main() already applies this
        # before validate_config in the normal launch path; repeating it here
        # (the helper clears the settings cache) keeps run_cli correct when
        # invoked directly, e.g. from tests or an embedding host.
        if not getattr(args, "keep_db_backend", False):
            _apply_fat_runtime_env()

        from nymeria import NymeriaAgent
        from nymeria.tools import SEED_TOOLS

        agent = NymeriaAgent(tools=list(SEED_TOOLS))
        agent.sync_agent_tools()

    # Start CLI. Explicit launch thread refs are resolved by the CLI after the
    # transport is selected so titles and ID prefixes can be matched safely.
    start_cli(
        agent=agent,
        thread_id=None if runtime_config.startup_thread_ref else args.thread,
        runtime_config=runtime_config,
    )


def _run_export(args: argparse.Namespace) -> int:
    """Non-interactive thread export via --export flag. Returns exit code."""
    import asyncio

    from nymeria.triggers.cli.transport.api import (
        DEFAULT_API_URL,
        APIAgentClient,
    )
    from nymeria.triggers.api_client import NymeriaAPIClient
    from nymeria.triggers.cli.commands.export import _handle_export
    from nymeria.triggers.cli.commands.base import CommandContext, ListCommandOutputSink

    thread_id = args.export
    fmt = getattr(args, "output_format", "json") or "json"
    output = getattr(args, "output", None)

    api_url = args.api_url or DEFAULT_API_URL
    api_key = args.api_key
    user_id = args.user_id or "default"

    async def do_export() -> int:
        api = NymeriaAPIClient(base_url=api_url, api_key=api_key)
        client = APIAgentClient(api, base_url=api_url, default_user_id=user_id)
        sink = ListCommandOutputSink()
        context = CommandContext(
            client=client,
            output=sink,
            thread_id=thread_id,
            user_id=user_id,
        )
        export_args = [fmt]
        if output:
            export_args.extend(["--output", output])
        result = await _handle_export(context, export_args)
        for msg in result.messages:
            stream = sys.stderr if msg.level == "error" else sys.stdout
            print(msg.content, file=stream)
        return 0 if result.ok else 1

    return asyncio.run(do_export())


def run_api(args: argparse.Namespace) -> None:
    """Run the REST API server."""
    from nymeria.triggers.api import run_api as start_api
    from nymeria.config import get_settings

    settings = get_settings()
    host = args.host or settings.api_host
    port = args.port or settings.api_port

    print(f"Starting NymeriaOS API server on {host}:{port}...")
    if settings.api_docs_enabled:
        print(f"  - Docs: http://{host}:{port}/docs")
        print(f"  - ReDoc: http://{host}:{port}/redoc")
    else:
        print("  - API docs: disabled (set NYMERIA_API_DOCS=true to enable)")
    if settings.redis_enabled and settings.redis_url:
        print(f"  - Redis event bus: {_redis_url_for_display(settings.redis_url)}")

    # Agent creation, Redis event bus, FCM, ticker-disable logic, and tool
    # sync are all handled by create_api_app() inside start_api().
    start_api(host=host, port=port)


def _slim_loopback_host(bind_host: str) -> str:
    """Return a concrete connectable host for in-process slim clients."""
    if bind_host in ("0.0.0.0", "::"):
        return "127.0.0.1"
    return bind_host


def _apply_slim_runtime_env(
    host: str,
    port: int,
    data_dir: Optional[str] = None,
    missed_work_policy: Optional[str] = None,
    active_execution_stale_minutes: Optional[int] = None,
) -> str:
    """Set env overrides for slim mode and return the loopback base URL.

    Slim mode collapses the Docker stack into one process: SQLite for
    persistence, no Redis event bus, the watchdog sweep inside the in-process
    ticker, and MCP mounted on the same FastAPI app. The env values are set BEFORE
    ``get_settings()`` is called so the cached Pydantic Settings instance
    sees the correct values. Any caller that has already imported settings
    must call ``get_settings.cache_clear()`` afterwards.
    """
    loopback_host = _slim_loopback_host(host)
    base_url = f"http://{loopback_host}:{port}"

    os.environ["DATABASE_BACKEND"] = "sqlite"
    os.environ["REDIS_ENABLED"] = "false"
    # Clearing REDIS_URL prevents the API from initializing the cross-process
    # event bus even if .env.docker has set one for the regular api command.
    os.environ.pop("REDIS_URL", None)
    os.environ["API_HOST"] = host
    os.environ["API_PORT"] = str(port)
    os.environ["NYMERIA_API_URL"] = base_url
    if data_dir:
        os.environ["NYMERIA_DATA_DIR"] = data_dir
    if missed_work_policy:
        os.environ["SCHEDULER_MISSED_WORK_POLICY"] = missed_work_policy
    if active_execution_stale_minutes is not None:
        os.environ["SCHEDULER_ACTIVE_EXECUTION_STALE_MINUTES"] = str(
            active_execution_stale_minutes
        )

    # If settings were loaded earlier (e.g. by `_load_environment()` callers
    # or argparse imports), reset the cache so the slim overrides take effect.
    try:
        from nymeria.config import get_settings

        get_settings.cache_clear()
    except Exception:
        pass

    return base_url


def _apply_fat_runtime_env() -> None:
    """Pin the embedded ("fat") CLI agent to local, isolated persistence.

    The in-process ``run.py cli --transport local`` CLI embeds a full
    ``NymeriaAgent`` whose checkpointer (``build_checkpointer_config``) and
    event-bus selection read the same ``Settings`` the Docker stack uses.
    Because ``_load_environment`` merges ``.env.docker`` (typically Postgres +
    Redis) into the process env at import, an unguarded fat CLI would silently
    persist conversations to Postgres and honor Redis settings instead of
    behaving as the self-contained, single-user offline REPL it is meant to be.

    This is the fat-mode analogue of ``_apply_slim_runtime_env``: it forces
    SQLite and disables Redis so a stray env file cannot silently change
    behavior. Unlike slim it sets NO ``API_HOST``/``API_PORT``/``NYMERIA_API_URL``,
    because fat mode binds no server. (The fat CLI's autonomous event stream
    does not depend on this Redis pin: ``run_cli`` never calls ``set_event_bus``,
    so ``get_event_bus()`` always returns the in-memory bus regardless of the
    Redis env. The pin is defense-in-depth for other settings consumers.) The
    env values are set BEFORE
    ``get_settings()`` is first cached (the caller invokes this before
    ``validate_config``); the cache is cleared defensively in case settings were
    already loaded. Callers skip this entirely when the user passes
    ``--keep-db-backend`` to deliberately share the configured backend.
    """
    os.environ["DATABASE_BACKEND"] = "sqlite"
    os.environ["REDIS_ENABLED"] = "false"
    # Clearing REDIS_URL prevents any settings consumer from reaching the
    # cross-process bus even if .env.docker set one for the regular api command.
    os.environ.pop("REDIS_URL", None)

    # If settings were loaded earlier (e.g. by `_load_environment()` callers or
    # argparse imports), reset the cache so the fat overrides take effect.
    try:
        from nymeria.config import get_settings

        get_settings.cache_clear()
    except Exception:
        pass


def _resolve_slim_port(args: argparse.Namespace) -> int:
    """The slim listen port: explicit --port, else API_PORT from the loaded
    config (``_load_environment`` already merged config.env/.env.docker into
    the process env), else 8000. Used by BOTH resolution sites (main()'s early
    env pin and run_slim); if only one changed, the early pin would force
    API_PORT=8000 into the env before run_slim resolves, silently defeating a
    configured port.
    """
    explicit = getattr(args, "port", None)
    if explicit:
        return int(explicit)
    env_port = (os.environ.get("API_PORT") or "").strip()
    if env_port.isdigit():
        return int(env_port)
    return 8000


def run_slim(args: argparse.Namespace) -> None:
    """Run the single-process slim launcher (API + ticker + MCP)."""
    host = getattr(args, "host", None) or "127.0.0.1"
    port = _resolve_slim_port(args)
    data_dir = getattr(args, "data_dir", None)
    missed_work_policy = getattr(args, "missed_work_policy", None)
    active_execution_stale_minutes = getattr(
        args, "active_execution_stale_minutes", None
    )
    enable_mcp = not getattr(args, "no_mcp", False)
    enable_watchdog = not getattr(args, "no_watchdog", False)
    if not enable_watchdog:
        # The watchdog is a ticker sub-loop now; the per-cycle env kill
        # switch is the single mechanism, so the debug flag just sets it.
        os.environ["NYMERIA_WATCHDOG_DISABLED"] = "1"

    base_url = _apply_slim_runtime_env(
        host,
        port,
        data_dir=data_dir,
        missed_work_policy=missed_work_policy,
        active_execution_stale_minutes=active_execution_stale_minutes,
    )

    from nymeria.triggers.api import run_api as start_api
    from nymeria.config import get_settings

    settings = get_settings()

    print(f"Starting NymeriaOS SLIM (single-process) on {host}:{port}...")
    print("  - Mode: SQLite + in-process ticker + embedded MCP")
    print(f"  - Internal API URL: {base_url}")
    print(f"  - Data directory: {settings.data_dir}")
    print(f"  - Missed work policy: {settings.scheduler_missed_work_policy}")
    print(
        "  - Active execution stale window: "
        f"{settings.scheduler_active_execution_stale_minutes}m"
    )
    if enable_mcp:
        print(f"  - MCP endpoint: {base_url}/mcp")
    else:
        print("  - MCP: disabled (--no-mcp)")
    if settings.watchdog_enabled and enable_watchdog:
        print(
            f"  - Watchdog: ticker sub-loop (interval={settings.watchdog_interval_minutes}m, "
            f"staleness={settings.todo_staleness_minutes}m)"
        )
    elif not settings.watchdog_enabled:
        print("  - Watchdog: disabled (WATCHDOG_ENABLED=false)")
    else:
        print("  - Watchdog: disabled (--no-watchdog)")
    if settings.api_docs_enabled:
        print(f"  - Docs: {base_url}/docs")

    start_api(
        host=host,
        port=port,
        slim_mode=True,
        slim_base_url=base_url,
        enable_slim_mcp=enable_mcp,
    )


def run_service(args: argparse.Namespace) -> None:
    """Handle service subcommand: manager actions, or the foreground gateway."""
    action = getattr(args, "action", None) or "run"
    if action == "run":
        run_gateway_foreground(args)
        return
    from nymeria.service_install import service_cli

    root_arg = getattr(args, "root", None)
    root = Path(root_arg).expanduser().resolve() if root_arg else None
    sys.exit(service_cli(action, root=root))


def run_init(args: argparse.Namespace) -> int:
    """Run first-time package setup."""
    from nymeria.setup import run_init as start_init

    return start_init(args)


def run_doctor(args: argparse.Namespace) -> int:
    """Run installation diagnostics."""
    from nymeria.doctor import run_doctor as start_doctor

    return start_doctor(args)


def _reembed_progress(done: int, total: int) -> None:
    print(f"  {done}/{total} chunks", end="\r", flush=True)


def run_reembed(args: argparse.Namespace) -> int:
    """Re-embed memory indexes after an embedding model/provider/dimension change.

    The vec0 vector width is fixed at table creation, so changing
    EMBEDDING_DIMENSIONS (or switching to a model with a different native width)
    requires dropping and rebuilding each user's vector table. This rebuilds with
    the currently-configured embedder and re-embeds every chunk; BM25 search keeps
    working throughout. Run it after editing the embedding settings in config.env.
    """
    from nymeria.config import get_settings
    from nymeria.core.memory_index import MemoryIndex

    settings = get_settings()
    users_dir = settings.data_dir / "users"
    if getattr(args, "user", None):
        targets = [(args.user, users_dir / args.user / "memory.db")]
    elif users_dir.exists():
        targets = [
            (path.name, path / "memory.db")
            for path in sorted(users_dir.iterdir())
            if (path / "memory.db").exists()
        ]
    else:
        targets = []
    targets = [(name, db) for name, db in targets if db.exists()]
    if not targets:
        print(f"No memory indexes found under {users_dir}.")
        return 0

    dim = settings.embedding_dimensions or 1536
    print(
        f"Re-embedding {len(targets)} memory index(es) with "
        f"provider={settings.embedding_provider}, model={settings.embedding_model}, "
        f"dim={dim}."
    )
    failed_any = False
    for name, db in targets:
        print(f"- {name}")
        result = MemoryIndex(db).rebuild_vectors(progress=_reembed_progress)
        print(
            f"  embedded {result['embedded']}/{result['total']} "
            f"(failed {result['failed']})" + " " * 12
        )
        failed_any = failed_any or bool(result["failed"])
    print(
        "Re-embed complete."
        if not failed_any
        else "Re-embed finished with failures (see above)."
    )
    return 1 if failed_any else 0


def run_worker(args: argparse.Namespace) -> None:
    """
    Run the worker (scheduler-only thin client for Docker deployments).

    The worker polls scheduled TODOs and poll-based trigger sources, but no
    longer constructs a ``NymeriaAgent``. Every turn it dispatches is
    relayed to the API container via ``POST /chat`` (with
    ``publish_autonomous_events=False`` so the worker stays the sole
    publisher of autonomous SSE bookends). This keeps the in-memory
    ``ThreadLockManager`` and ``PendingPromptQueue`` process-local to the
    one process that runs the agent — the API.
    """
    import asyncio
    import time
    import urllib.error
    import urllib.request

    from nymeria.config import get_settings
    from nymeria.core.event_bus import create_event_bus, set_event_bus
    from nymeria.core.service_health import write_service_heartbeat
    from nymeria.core.thread_config import ThreadConfigManager
    from nymeria.core.ticker import Ticker
    from nymeria.core.todo_manager import TodoManager
    from nymeria.core.todo_schedule_db import TodoScheduleDB
    from nymeria.core.turn_executor import APIClientExecutor
    from nymeria.core.user_profile import UserProfileManager

    settings = get_settings()

    # The service token is resolved AFTER the API health wait below, not here:
    # in the full Docker stack the api self-mints it onto the shared data volume
    # during its own startup, and the worker depends only on postgres/redis (not
    # api-healthy), so it can reach this point before the token exists.

    api_url = (
        getattr(args, "api_url", None)
        or os.environ.get("NYMERIA_API_URL")
        or "http://nymeria-api:8000"
    )

    print("Starting Nymeria Worker (scheduler-only thin client)...")
    print(f"  - API URL: {api_url}")
    print(f"  - Ticker poll interval: {settings.ticker_poll_interval}s")
    print(f"  - Watchdog enabled: {settings.watchdog_enabled}")
    print(f"  - Redis enabled: {settings.redis_enabled}")
    print(f"  - Data directory: {settings.data_dir}")

    # Initialize Redis event bus if configured (worker still publishes
    # task_started / agent stream chunks / task_completed for the TODOs
    # and triggers it dispatches; the API call carries
    # publish_autonomous_events=False so we don't get duplicates).
    # Publisher-only mode skips the pub/sub subscriber thread: the worker
    # never reads events back, and the API container is the sole
    # subscriber. Avoids the idle socket-timeout warning loop on a
    # subscriber that nothing consumes from.
    if settings.redis_enabled and settings.redis_url:
        event_bus = create_event_bus(settings, enable_subscriber=False)
        set_event_bus(event_bus)
        print(
            f"  - Redis event bus (publisher-only): "
            f"{_redis_url_for_display(settings.redis_url)}"
        )

    # Initialize FCM if enabled (for autonomous notifications, dispatched
    # by the same notification helpers the ticker uses).
    if settings.fcm_enabled and settings.fcm_credentials_json:
        from nymeria.core.fcm import _init_firebase
        if _init_firebase(settings.fcm_credentials_json):
            print("  - FCM push notifications: enabled")

    # Wait for the API container to become healthy before firing TODOs (and,
    # in the full Docker stack, before resolving the service token the api
    # self-mints onto the shared volume during its own startup). The worker
    # depends only on postgres/redis, so on a first `up -d` that BUILDS the
    # image the api can be absent for minutes; a generous deadline avoids a
    # crash-restart loop (restart: unless-stopped remains the backstop for a
    # genuinely-down API). Without this, a recovered TODO would also burn the
    # ticker's retry budget on connection-refused.
    health_url = api_url.rstrip("/") + "/health"
    health_wait_seconds = 180
    deadline = time.monotonic() + health_wait_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=2) as resp:
                if 200 <= resp.status < 300:
                    print("  - API health: ok")
                    break
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    else:
        print(
            f"  - API health: unreachable after {health_wait_seconds}s — "
            "continuing anyway, the ticker will retry per-TODO until the API "
            "comes up."
        )

    # Now that we have waited for the API, resolve the service token: env var if
    # the operator set one, else the file the api self-minted onto the shared
    # volume. Exits with provisioning guidance if neither yields a token; a
    # transient miss self-heals via the worker's ``restart: unless-stopped``.
    service_token = _require_service_token(settings, "the worker (ticker)")
    client = _service_api_client(api_url, service_token, settings)
    executor = APIClientExecutor(client, publish_autonomous_events=False)

    schedule_db = TodoScheduleDB(settings.data_dir / "todo_schedule.db")
    todo_manager = TodoManager(settings.data_dir)
    thread_config_manager = ThreadConfigManager(settings.data_dir)
    profile_manager = UserProfileManager(settings.data_dir)

    ticker = Ticker(
        executor=executor,
        settings=settings,
        schedule_db=schedule_db,
        todo_manager=todo_manager,
        thread_config_manager=thread_config_manager,
        profile_manager=profile_manager,
        poll_interval=settings.ticker_poll_interval,
        busy_agent=None,
        spawn_sweeper=None,
    )

    # Rebuild schedule index and recover missed schedules on startup,
    # matching what NymeriaAgent.__init__ does in slim mode.
    startup_status = ticker.prepare_startup_recovery()
    indexed = int(startup_status.get("indexed_schedule_count") or 0)
    if indexed > 0:
        print(f"  - Indexed {indexed} scheduled TODO(s)")
    recovered = int(startup_status.get("startup_missed_count") or 0)
    if recovered > 0:
        print(f"  - Found {recovered} missed scheduled TODO(s)")

    ticker.start()

    def write_worker_heartbeat() -> None:
        ticker_thread = getattr(ticker, "_thread", None)
        ticker_running = bool(
            getattr(ticker, "_running", False)
            and ticker_thread is not None
            and ticker_thread.is_alive()
        )
        write_service_heartbeat(
            "worker",
            status="ok" if ticker_running else "unhealthy",
            details={
                "ticker_running": ticker_running,
                "ticker_thread_alive": bool(
                    ticker_thread is not None and ticker_thread.is_alive()
                ),
                "poll_interval_seconds": settings.ticker_poll_interval,
                "api_url": api_url,
                "watchdog": ticker.watchdog_stats(),
            },
        )

    write_worker_heartbeat()

    async def _close_executor() -> None:
        try:
            await executor.aclose()
        except Exception:
            pass  # Best-effort during shutdown.

    def _on_shutdown() -> None:
        ticker.stop()
        try:
            asyncio.run(_close_executor())
        except RuntimeError:
            # An asyncio loop may already be torn down; ignore.
            pass

    # Hard exit: ThreadPoolExecutor threads are non-daemon and would otherwise
    # keep the process alive indefinitely.
    _install_exit_handlers(
        "\nShutdown signal received, stopping ticker...", on_stop=_on_shutdown
    )

    print("\nWorker running. Press Ctrl+C to stop.")

    try:
        while True:
            write_worker_heartbeat()
            time.sleep(10)
    except KeyboardInterrupt:
        ticker.stop()
        try:
            asyncio.run(_close_executor())
        except RuntimeError:
            pass
        print("\nWorker stopped.")


def run_discord_bot(args: argparse.Namespace) -> None:
    """
    Run the Discord bot (gateway mode via WebSocket).

    Thin client architecture: the bot calls the Nymeria REST API for
    all operations instead of running its own NymeriaAgent. This
    ensures Discord always reflects the same state as the frontend.
    """
    from nymeria.config import get_settings
    from nymeria.triggers import discord_bot as _discord_bot
    _require_bot_sdk(_discord_bot, "Discord", "discord")
    from nymeria.triggers.discord_bot import NymeriaDiscordBot

    settings = get_settings()

    if not settings.discord_bot_token:
        print("\n[Error] DISCORD_BOT_TOKEN is not set.")
        print("  1. Create a bot at https://discord.com/developers/applications")
        print("  2. Copy the bot token and add it to your .env file:")
        print("     DISCORD_BOT_TOKEN=your-token-here")
        sys.exit(1)

    # API URL is required — the bot is a thin client
    api_url = _resolve_api_url(args)
    # Bots authenticate as the bot-service admin and route per-user traffic
    # with X-Nymeria-Act-As.
    api_key = _require_service_token(settings, "the Discord bot")

    print("Starting Nymeria Discord Bot (thin client)...")
    print("  - Mode: gateway (WebSocket)")
    print(f"  - Respond mode: {settings.discord_respond_mode}")
    print(f"  - API: {api_url}")
    print("  - Auth: service token")

    # Create API client
    api = _service_api_client(api_url, api_key, settings)

    # Create and run bot
    bot = NymeriaDiscordBot(
        api=api,
        respond_mode=settings.discord_respond_mode,
    )

    _install_exit_handlers("\nShutdown signal received, stopping Discord bot...")

    print("\nConnecting to Discord...")
    bot.run(settings.discord_bot_token, log_handler=None)


def run_telegram_bot(args: argparse.Namespace) -> None:
    """
    Run the Telegram bot (polling mode).

    Thin client architecture: the bot calls the Nymeria REST API for
    all operations instead of running its own NymeriaAgent. This
    ensures Telegram always reflects the same state as the frontend.
    """
    from nymeria.config import get_settings
    from nymeria.triggers import telegram_bot as _telegram_bot
    _require_bot_sdk(_telegram_bot, "Telegram", "telegram")
    from nymeria.triggers.telegram_bot import NymeriaTelegramBot

    settings = get_settings()

    if not settings.telegram_bot_token:
        print("\n[Error] TELEGRAM_BOT_TOKEN is not set.")
        print("  1. Message @BotFather on Telegram")
        print("  2. Create a new bot with /newbot")
        print("  3. Copy the token and add it to your .env:")
        print("     TELEGRAM_BOT_TOKEN=your-token-here")
        sys.exit(1)

    # API URL is required — the bot is a thin client
    api_url = _resolve_api_url(args)
    api_key = _require_service_token(settings, "the Telegram bot")

    print("Starting Nymeria Telegram Bot (thin client)...")
    print("  - Mode: polling")
    print(f"  - API: {api_url}")
    if settings.telegram_default_chat_id:
        print(f"  - Default chat: {settings.telegram_default_chat_id}")
    print("  - Auth: service token")

    # Create API client
    api = _service_api_client(api_url, api_key, settings)

    # Create and run bot
    bot = NymeriaTelegramBot(
        api=api,
        bot_token=settings.telegram_bot_token,
        default_chat_id=settings.telegram_default_chat_id,
    )

    _install_exit_handlers("\nShutdown signal received, stopping Telegram bot...")

    print("\nConnecting to Telegram...")
    bot.run()


def run_slack_bot(args: argparse.Namespace) -> None:
    """
    Run the Slack bot (Socket Mode).

    Thin client architecture: the bot calls the Nymeria REST API for all
    operations instead of running its own NymeriaAgent.
    """
    from nymeria.config import get_settings
    from nymeria.triggers import slack_bot as _slack_bot
    _require_bot_sdk(_slack_bot, "Slack", "slack")
    from nymeria.triggers.slack_bot import NymeriaSlackBot

    settings = get_settings()

    if not settings.slack_bot_token:
        print("\n[Error] SLACK_BOT_TOKEN is not set.")
        print("  1. Create a Slack app at https://api.slack.com/apps")
        print("  2. Install it to your workspace and copy the Bot User OAuth Token:")
        print("     SLACK_BOT_TOKEN=xoxb-...")
        sys.exit(1)
    if not settings.slack_app_token:
        print("\n[Error] SLACK_APP_TOKEN is not set.")
        print("  1. Enable Socket Mode for the Slack app")
        print("  2. Generate an app-level token with connections:write:")
        print("     SLACK_APP_TOKEN=xapp-...")
        sys.exit(1)

    api_url = _resolve_api_url(args)
    api_key = _require_service_token(settings, "the Slack bot")

    print("Starting Nymeria Slack Bot (thin client)...")
    print("  - Mode: Socket Mode")
    print(f"  - Respond mode: {settings.slack_respond_mode}")
    print(f"  - API: {api_url}")
    print("  - Auth: service token")

    api = _service_api_client(api_url, api_key, settings)
    bot = NymeriaSlackBot(
        api=api,
        bot_token=settings.slack_bot_token,
        app_token=settings.slack_app_token,
        respond_mode=settings.slack_respond_mode,
        show_tool_events=settings.slack_show_tool_events,
    )

    _install_exit_handlers("\nShutdown signal received, stopping Slack bot...")

    print("\nConnecting to Slack...")
    bot.run()


def run_mcp(args: argparse.Namespace) -> None:
    """Run the MCP server."""
    from nymeria.mcp_server import run_stdio, run_http

    api_url = getattr(args, "api_url", None)
    if args.http:
        host = args.host or "127.0.0.1"
        port = args.port or 8001
        api_label = api_url or os.environ.get("NYMERIA_API_URL") or "auto"
        print(f"Starting Nymeria MCP server (HTTP mode) on {host}:{port}; API={api_label}...")
        run_http(host=host, port=port, api_url=api_url)
    else:
        # STDIO mode - minimal output to avoid corrupting JSON-RPC
        run_stdio(api_url=api_url)


def run_claude_code_runner(args: argparse.Namespace) -> None:
    """Run the host-side Claude Code runner for the claude_code bridge.

    A standalone HTTP service that executes Claude Code on the host where the
    repo and real auth live. It needs no Nymeria DB or service token; it reads
    only the NYMERIA_CLAUDE_CODE_* env. Bind it to a private interface only.
    """
    from nymeria.gateway.claude_code_runner import serve

    host = args.host or "127.0.0.1"
    port = args.port or 8200
    print(
        f"Starting Nymeria Claude Code runner on {host}:{port} "
        f"(insecure={bool(args.insecure)})..."
    )
    _install_exit_handlers("\nShutdown signal received, stopping Claude Code runner...")
    serve(host=host, port=port, allow_insecure=bool(args.insecure))


def run_completion(args: argparse.Namespace) -> None:
    """Generate and print a shell completion script."""
    from nymeria.triggers.cli.completion import generate

    parser = build_parser()
    print(generate(args.shell, parser))


def run_gateway_foreground(args: argparse.Namespace) -> None:
    """
    Run the gateway server in foreground mode for debugging.

    This is similar to what the Windows service does, but runs in the
    current console with proper signal handling for Ctrl+C.
    """
    from nymeria.config import get_settings

    settings = get_settings()

    # Use standard logging (console + file) — same as api/worker
    setup_logging(settings.log_level)

    print("Starting Nymeria Gateway in foreground mode...")
    print(f"  REST API: http://{settings.api_host}:{settings.api_port}")
    print(f"  Log file: {settings.logs_dir / settings.service_log_file}")
    print("  Press Ctrl+C to stop")
    print()

    from nymeria.gateway.server import GatewayServer

    gateway = GatewayServer(settings=settings)

    _install_exit_handlers(
        "\nShutdown signal received...", on_stop=gateway.stop, hard_exit=False
    )

    # Start the gateway
    gateway.start()

    # Wait for shutdown
    gateway.wait_for_stop()

    print("Gateway stopped")


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level launcher parser."""
    from nymeria.setup import add_init_arguments

    parser = argparse.ArgumentParser(
        description="Nymeria - Personal AI Assistant",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python run.py cli                   # Start CLI interface
    python run.py cli -c                 # Resume most recent thread
    python run.py cli -r mythread        # Resume thread by ID or title
    python run.py cli -r "Project Plan"  # Resume thread by title substring
    python run.py cli -c -m "hello"      # Oneshot message to most recent thread
    python run.py cli -r abc -m "hello"  # Oneshot message to specific thread
    python run.py api                # Start API server (default port 8000)
    python run.py api -p 8080        # Start API on port 8080
    python run.py slim               # Single-process local launcher (no Docker/Redis/Postgres)
    python run.py doctor             # Diagnose local configuration
    python run.py discord-bot       # Start Discord bot (gateway mode)
    python run.py mcp                # Start MCP server (STDIO mode)
    python run.py mcp --http         # Start MCP server (HTTP mode)
    python run.py mcp --http -p 8001 # MCP HTTP mode on custom port
    python run.py service            # Run gateway in foreground
        """,
    )
    parser.add_argument(
        "--log-level",
        "-l",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging level",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # CLI subcommand
    cli_parser = subparsers.add_parser("cli", help="Start CLI interface")
    cli_parser.add_argument(
        "thread_ref",
        nargs="*",
        help=(
            "(Deprecated — use -r/--resume instead.) "
            "Thread title or ID/prefix to open. Use 'list' to list threads and exit."
        ),
    )
    cli_parser.add_argument(
        "--thread",
        "--thread-id",
        "-t",
        dest="thread",
        default=None,
        help="(Deprecated — use -r/--resume instead.) Thread title or ID/prefix to open",
    )
    cli_parser.add_argument(
        "--continue",
        "-c",
        dest="continue_last",
        action="store_true",
        default=False,
        help="Resume the most recently updated thread",
    )
    cli_parser.add_argument(
        "--resume",
        "-r",
        dest="resume_ref",
        default=None,
        metavar="ID_OR_TITLE",
        help="Resume a specific thread by ID prefix or title substring",
    )
    # --transport is the canonical selector; --thin/--fat are convenience
    # aliases. They share dest="transport" and live in a mutually exclusive
    # group so contradictory combinations (e.g. --fat --thin, or
    # --fat --transport api) are rejected. --transport is added first so its
    # default="api" is the namespace default (argparse only sets a dest default
    # from the first action that declares one).
    transport_group = cli_parser.add_mutually_exclusive_group()
    transport_group.add_argument(
        "--transport",
        choices=("api", "local", "auto"),
        default=None,
        help=(
            "Transport mode for CLI chat and command requests "
            "(default: api). Aliases: --thin (=api), --fat (=local)."
        ),
    )
    transport_group.add_argument(
        "--fat",
        dest="transport",
        action="store_const",
        const="local",
        help=(
            "Run an embedded in-process agent (alias for --transport local). "
            "Tools execute on this machine; no backend server required."
        ),
    )
    transport_group.add_argument(
        "--thin",
        dest="transport",
        action="store_const",
        const="api",
        help=(
            "Connect to a running backend over HTTP "
            "(alias for --transport api; the default)."
        ),
    )
    cli_parser.add_argument(
        "--keep-db-backend",
        action="store_true",
        default=False,
        help=(
            "Fat mode only (--transport local / --fat): keep the configured "
            "DATABASE_BACKEND and REDIS_* values from your environment instead "
            "of forcing local SQLite with Redis off. Use only when you "
            "deliberately want the embedded agent to share a Postgres/Redis "
            "backend; the default keeps fat mode a self-contained offline REPL."
        ),
    )
    cli_parser.add_argument(
        "--renderer",
        choices=("rich", "plain", "auto"),
        default="rich",
        help="Renderer mode for CLI terminal output (default: rich)",
    )
    cli_parser.add_argument(
        "--api-url",
        default=None,
        help="Nymeria API URL for API transport mode",
    )
    cli_parser.add_argument(
        "--api-key",
        default=None,
        help="Nymeria API key for API transport mode",
    )
    cli_parser.set_defaults(user_id_explicit=False)
    cli_parser.add_argument(
        "--user-id",
        default="default",
        action=_StoreUserIdAction,
        help="User ID for CLI requests (default: default)",
    )
    cli_parser.add_argument(
        "--message",
        "-m",
        default=None,
        help=(
            "Send a single message, stream the response to stdout, and exit. "
            "Use '-' to read from stdin."
        ),
    )
    cli_parser.add_argument(
        "--format",
        dest="output_format",
        choices=("plain", "json", "md", "jsonl"),
        default="plain",
        help="Output format for oneshot (plain|json) or --export (json|md|jsonl)",
    )
    cli_parser.add_argument(
        "--export",
        default=None,
        metavar="THREAD_ID",
        help="Export a thread to a file and exit (use with --format and --output)",
    )
    cli_parser.add_argument(
        "--output",
        "-o",
        default=None,
        metavar="PATH",
        help="Output file path for --export (default: auto-generated)",
    )
    cli_parser.add_argument(
        "--no-animation",
        dest="animation",
        action="store_false",
        default=True,
        help="Disable spinner/status animation in CLI renderers",
    )
    cli_parser.add_argument(
        "--ascii",
        dest="ascii_only",
        action="store_true",
        help="Prefer ASCII-only CLI output in renderers",
    )
    cli_parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="Color output policy for future CLI renderers (default: auto)",
    )
    rich_scroll_region_group = cli_parser.add_mutually_exclusive_group()
    rich_scroll_region_group.add_argument(
        "--rich-scroll-region",
        dest="rich_scroll_region",
        action="store_true",
        default=None,
        help=(
            "Use the default Rich REPL native scrollback follow-footer renderer "
            "(also NYMERIA_CLI_RICH_SCROLL_REGION=1)"
        ),
    )
    rich_scroll_region_group.add_argument(
        "--no-rich-scroll-region",
        dest="rich_scroll_region",
        action="store_false",
        help="Disable the default Rich REPL scroll-region follow-footer renderer",
    )

    # API subcommand
    api_parser = subparsers.add_parser("api", help="Start REST API server")
    api_parser.add_argument(
        "--host",
        "-H",
        default=None,
        help="Host to bind to (default from settings)",
    )
    api_parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=None,
        help="Port to listen on (default from settings)",
    )

    # Slim subcommand — single-process local launcher
    slim_parser = subparsers.add_parser(
        "slim",
        help="Start single-process local launcher (API + ticker + MCP)",
    )
    slim_parser.add_argument(
        "--host",
        "-H",
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1)",
    )
    slim_parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=None,
        help="Port to listen on (default: API_PORT from the config, else 8000)",
    )
    slim_parser.add_argument(
        "--data-dir",
        default=None,
        help="Runtime data directory (writes NYMERIA_DATA_DIR before settings load)",
    )
    slim_parser.add_argument(
        "--missed-work-policy",
        choices=("run", "ask"),
        default=None,
        help=(
            "Startup handling for scheduled TODOs missed while offline: "
            "run immediately, or ask by holding them until released"
        ),
    )
    slim_parser.add_argument(
        "--active-execution-stale-minutes",
        type=int,
        default=None,
        help=(
            "Minutes before a crashed scheduled-TODO execution marker is "
            "considered stale"
        ),
    )
    slim_parser.add_argument(
        "--no-mcp",
        action="store_true",
        help="Skip mounting the embedded MCP server at /mcp",
    )
    slim_parser.add_argument(
        "--no-watchdog",
        action="store_true",
        help=(
            "Skip the watchdog ticker sub-loop even when WATCHDOG_ENABLED=true "
            "(sets NYMERIA_WATCHDOG_DISABLED for this process)"
        ),
    )

    # Worker subcommand
    worker_parser = subparsers.add_parser(
        "worker",
        help="Start worker (ticker only, for Docker deployments)"
    )
    worker_parser.add_argument(
        "--api-url",
        default=None,
        help=(
            "URL of the Nymeria API container the worker relays TODO and "
            "trigger turns to (default: $NYMERIA_API_URL or "
            "http://nymeria-api:8000)"
        ),
    )

    # Init subcommand
    init_parser = subparsers.add_parser(
        "init",
        help="Create first-time config.env and data directory",
    )
    add_init_arguments(init_parser)

    # Doctor subcommand
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Diagnose Nymeria configuration, storage, and optional services",
    )
    doctor_parser.add_argument(
        "--skip-llm-test",
        action="store_true",
        help="Skip the live provider connection check",
    )

    # Re-embed subcommand
    reembed_parser = subparsers.add_parser(
        "reembed",
        help="Re-embed memory indexes after an embedding model/dimension change",
    )
    reembed_parser.add_argument(
        "--user",
        default=None,
        help="Re-embed only this user's memory index (default: all users)",
    )

    # Discord bot subcommand
    discord_parser = subparsers.add_parser(
        "discord-bot",
        help="Start Discord bot (gateway mode)"
    )
    _add_api_url_arg(
        discord_parser,
        help_text=(
            "URL of running Nymeria API (e.g. http://localhost:8000). "
            "Enables autonomous task results to appear in Discord channels "
            "when running the bot and API as separate local processes."
        ),
    )

    # Telegram bot subcommand
    telegram_parser = subparsers.add_parser(
        "telegram-bot",
        help="Start Telegram bot (polling mode)"
    )
    _add_api_url_arg(telegram_parser)

    # Slack bot subcommand
    slack_parser = subparsers.add_parser(
        "slack-bot",
        help="Start Slack bot (Socket Mode)",
    )
    _add_api_url_arg(slack_parser)

    # MCP subcommand
    mcp_parser = subparsers.add_parser("mcp", help="Start MCP server for agent-to-agent communication")
    mcp_parser.add_argument(
        "--http",
        action="store_true",
        help="Use HTTP transport instead of STDIO",
    )
    mcp_parser.add_argument(
        "--host",
        "-H",
        default=None,
        help="Host to bind to for HTTP mode (default: 127.0.0.1)",
    )
    mcp_parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=None,
        help="Port for HTTP mode (default: 8001)",
    )
    mcp_parser.add_argument(
        "--api-url",
        default=None,
        help="URL of the running Nymeria API (default: NYMERIA_API_URL, Docker nymeria-api, or localhost:8000)",
    )

    # Claude Code runner subcommand (host-side bridge service)
    cc_runner_parser = subparsers.add_parser(
        "claude-code-runner",
        help="Run the host-side Claude Code runner for the claude_code bridge",
        description=(
            "Execute Claude Code on the host for the Nymeria claude_code tool. "
            "Reads NYMERIA_CLAUDE_CODE_* env (token, allowlist, model, budgets). "
            "Bind to a PRIVATE interface only (loopback or the Docker bridge); "
            "never expose it publicly."
        ),
    )
    cc_runner_parser.add_argument(
        "--host", "-H", default=None,
        help="Host/interface to bind (default: 127.0.0.1). Use the docker-bridge IP to serve containers.",
    )
    cc_runner_parser.add_argument(
        "--port", "-p", type=int, default=None, help="Port to bind (default: 8200)",
    )
    cc_runner_parser.add_argument(
        "--insecure", action="store_true",
        help="Allow running without a bearer token (loopback-only development).",
    )

    # Service subcommand: background-service manager + foreground gateway
    service_parser = subparsers.add_parser(
        "service",
        help="Manage the background service (install/uninstall/status/restart), or run the foreground gateway",
        description=(
            "Manage the slim backend's background service: a systemd user unit "
            "on Linux, a launchd agent on macOS. With no action (or `run`), "
            "start the GatewayServer (REST transport) in the foreground with "
            "graceful Ctrl+C shutdown."
        ),
    )
    service_parser.add_argument(
        "action",
        nargs="?",
        choices=["install", "uninstall", "status", "restart", "run"],
        default="run",
        help=(
            "install: write the unit/agent, enable it, start it, and verify "
            "health; uninstall: stop and remove it; status: service state plus "
            "a health probe; restart: restart the service; run: foreground "
            "gateway (default)"
        ),
    )
    service_parser.add_argument(
        "--root",
        default=None,
        help=(
            "Project root the service should run against (where config.env and "
            "data live). Defaults to NYMERIA_PROJECT_ROOT / auto-discovery; the "
            "setup wizard prints the matching --root for its config."
        ),
    )

    # Users subcommand (account provisioning)
    from nymeria.cli import users as users_cli
    users_cli.build_parser(subparsers)

    # Snapshot subcommand (user-data backup/restore)
    from nymeria.cli import snapshot as snapshot_cli
    snapshot_cli.build_parser(subparsers)

    # Completion subcommand
    from nymeria.triggers.cli.completion import SUPPORTED_SHELLS
    completion_parser = subparsers.add_parser(
        "completion",
        help="Generate shell completion scripts",
    )
    completion_parser.add_argument(
        "shell",
        choices=SUPPORTED_SHELLS,
        help="Target shell (bash, zsh, or fish)",
    )

    return parser


def run_users(args: argparse.Namespace) -> int:
    """Dispatch a ``users`` subcommand against the local accounts DB."""
    from nymeria.cli import users as users_cli

    return users_cli.dispatch(args)


def run_snapshot(args: argparse.Namespace) -> int:
    """Dispatch a ``snapshot`` subcommand against local state (no API needed)."""
    from nymeria.cli import snapshot as snapshot_cli

    return snapshot_cli.dispatch(args)


class _Command(NamedTuple):
    """How ``main`` runs and validates one top-level subcommand.

    runner:          the ``run_*`` function for the command.
    exits:           call ``sys.exit(runner(args))`` (commands that return an
                     exit code) instead of ``runner(args)``.
    full_validation: part of the server/bot set that runs the standard
                     ``validate_config(...)`` gate. ``cli``, ``service``, and
                     ``users`` validate conditionally and are handled explicitly
                     in ``main``; everything else
                     (init/doctor/reembed/snapshot/completion) runs no config
                     validation.
    """

    runner: Callable[[argparse.Namespace], Optional[int]]
    exits: bool = False
    full_validation: bool = False


# Single source of truth mapping a subcommand to its runner and validation
# behavior. Adding a command here (plus its subparser in build_parser) wires up
# both dispatch and config validation; the two can no longer drift.
COMMANDS: dict[str, _Command] = {
    "cli": _Command(run_cli),
    "api": _Command(run_api, full_validation=True),
    "slim": _Command(run_slim, full_validation=True),
    "worker": _Command(run_worker, full_validation=True),
    "init": _Command(run_init, exits=True),
    "doctor": _Command(run_doctor, exits=True),
    "reembed": _Command(run_reembed, exits=True),
    "discord-bot": _Command(run_discord_bot, full_validation=True),
    "telegram-bot": _Command(run_telegram_bot, full_validation=True),
    "slack-bot": _Command(run_slack_bot, full_validation=True),
    "mcp": _Command(run_mcp, full_validation=True),
    "claude-code-runner": _Command(run_claude_code_runner),
    "service": _Command(run_service),
    "users": _Command(run_users, exits=True),
    "snapshot": _Command(run_snapshot, exits=True),
    "completion": _Command(run_completion),
}

_FULL_VALIDATION_COMMANDS = frozenset(
    name for name, command in COMMANDS.items() if command.full_validation
)


def main() -> None:
    """Main entry point."""
    _suppress_runtime_dependency_warnings()
    parser = build_parser()
    args = parser.parse_args()
    service_token_required = _service_token_requirement(args) is not None

    # Slim mode applies its env overrides BEFORE any settings cache load so the
    # validator and the API both see SQLite/Redis-off/loopback values.
    if args.command == "slim":
        _apply_slim_runtime_env(
            host=getattr(args, "host", None) or "127.0.0.1",
            port=_resolve_slim_port(args),
            data_dir=getattr(args, "data_dir", None),
            missed_work_policy=getattr(args, "missed_work_policy", None),
            active_execution_stale_minutes=getattr(
                args, "active_execution_stale_minutes", None
            ),
        )
    # Fat (in-process) CLI embeds a full agent; pin its persistence to local
    # SQLite (Redis off) BEFORE validate_config caches settings, unless the user
    # explicitly opts to share the configured backend. Mirrors the slim early
    # pin above. Only `--transport local` (incl. its `--fat` alias) builds the
    # embedded agent; api/auto never do.
    elif args.command == "cli" and getattr(args, "transport", None) == "local":
        if not getattr(args, "keep_db_backend", False):
            _apply_fat_runtime_env()

    # Setup logging (except for service commands and STDIO MCP, which must keep
    # stdout reserved for JSON-RPC messages; the MCP server configures stderr
    # logging internally so client transports are not corrupted. doctor and
    # snapshot are skipped too: they print operator-facing check reports and
    # must not interleave them with log lines.
    if args.command not in ("service", "mcp", "init", "doctor", "snapshot"):
        setup_logging(args.log_level)

    if service_token_required:
        from nymeria.config import get_settings
        _require_launch_mode_service_token(args, get_settings())

    # Slim bootstraps its own internal service token, so the generic
    # "NYMERIA_SERVICE_TOKEN not set" warning is misleading there. The worker is
    # the same: in the full Docker stack the api self-mints the token onto the
    # shared volume and the worker resolves it after its API health wait, so the
    # warning would be a false alarm (and "worker" is deliberately not in
    # _SERVICE_TOKEN_REQUIRED_COMMANDS, hence not covered by service_token_required).
    suppress_service_token_warning = (
        service_token_required
        or args.command == "slim"
        or args.command == "worker"
    )

    # Validate configuration before running commands that need it
    # Skip validation for service status checks and help
    if args.command == "cli":
        if getattr(args, "transport", "api") == "local":
            validate_config(suppress_service_token_warning=suppress_service_token_warning)
    elif args.command in _FULL_VALIDATION_COMMANDS:
        validate_config(suppress_service_token_warning=suppress_service_token_warning)
    elif args.command == "service":
        # Only the foreground gateway run needs a valid config; the service
        # manager actions must work before (install) or without (status,
        # uninstall) a complete configuration.
        if (getattr(args, "action", None) or "run") == "run":
            validate_config(suppress_service_token_warning=suppress_service_token_warning)
    elif args.command == "users":
        # Account CLI operates on the local DB directly; skip NYMERIA_API_KEY
        # check so the admin can provision users before the API is configured.
        validate_config(skip_api_key=True)
    # `snapshot` deliberately runs NO config validation (like doctor): a
    # disaster-recovery restore must work on a bare, half-configured host
    # without validate_config offering to launch the setup wizard. The
    # snapshot CLI loads settings itself and reports config problems in its
    # own terms.

    # Run appropriate command via the COMMANDS registry. Resolve the runner
    # through the module namespace at call time (by name) rather than calling
    # the reference captured in COMMANDS at import time, so tests that
    # monkeypatch a runner (e.g. run_slim/run_cli) still intercept dispatch,
    # exactly as the previous if/elif chain (which looked up the global by name)
    # did.
    command = COMMANDS.get(args.command)
    if command is None:
        parser.print_help()
        sys.exit(1)
    runner = globals()[command.runner.__name__]
    if command.exits:
        sys.exit(runner(args))
    runner(args)


if __name__ == "__main__":
    main()
