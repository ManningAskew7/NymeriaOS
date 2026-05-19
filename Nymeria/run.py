#!/usr/bin/env python
"""
Nymeria - Entry point script.

Usage:
    python run.py cli          # Start CLI interface
    python run.py api          # Start REST API server
    python run.py api --port 8080  # Start API on custom port
    python run.py slim         # Start single-process local launcher (API + MCP + watchdog)
    python run.py doctor           # Diagnose local configuration
    python run.py worker           # Start worker (ticker only, for Docker)
    python run.py discord-bot     # Start Discord bot (gateway mode)
    python run.py telegram-bot    # Start Telegram bot (polling mode)
    python run.py slack-bot       # Start Slack bot (Socket Mode)
    python run.py matrix-bot      # Start Matrix bot (sync loop)
    python run.py mattermost-bot  # Start Mattermost bot (WebSocket mode)
    python run.py zulip-bot       # Start Zulip bot (event queue mode)
    python run.py rocketchat-bot  # Start Rocket.Chat bot (realtime mode)
    python run.py signal-bot      # Start Signal bot (signal-cli SSE mode)
    python run.py twitch-bot      # Start Twitch chat bot
    python run.py mcp              # Start MCP server (stdio mode)
    python run.py mcp --http       # Start MCP server (HTTP mode)
    python run.py mcp --port 8001  # MCP HTTP mode on custom port
    python run.py service run      # Run gateway in foreground (debug)
"""

import argparse
import logging
import signal
import sys
import warnings
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

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
    "worker": "the worker (ticker)",
    "discord-bot": "the Discord bot",
    "telegram-bot": "the Telegram bot",
    "slack-bot": "the Slack bot",
    "matrix-bot": "the Matrix bot",
    "mattermost-bot": "the Mattermost bot",
    "zulip-bot": "the Zulip bot",
    "rocketchat-bot": "the Rocket.Chat bot",
    "signal-bot": "the Signal bot",
    "watchdog": "the watchdog worker",
    "twitch-bot": "the Twitch bot",
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
    errors, warnings = settings.validate()

    # Filter out API key error if skip_api_key is True
    if skip_api_key:
        errors = [e for e in errors if "NYMERIA_API_KEY" not in e]
    if suppress_service_token_warning:
        warnings = [w for w in warnings if "NYMERIA_SERVICE_TOKEN not set" not in w]

    # Print warnings (non-fatal)
    if warnings:
        print("\n[Configuration Warnings]")
        print("-" * 50)
        for warning in warnings:
            print(f"  [!] {warning}")
        print()

    # Print errors and exit if any critical issues
    if errors:
        print("\n[Configuration Error]")
        print("-" * 50)
        print("Nymeria cannot start due to missing configuration:\n")
        for error in errors:
            print(f"  [X] {error}\n")
        print("-" * 50)
        print("\nQuick Setup:")
        print("  1. Copy .env.docker.example to .env.docker, or create .env manually")
        print("  2. Add your LLM provider API key")
        print("  3. Run again: python run.py api")
        print("  4. On first boot, use data/BOOTSTRAP_TOKEN.txt to sign in")
        print("\nSee docs/QUICKSTART.md for detailed instructions.")
        sys.exit(1)


def _service_token_requirement(args: argparse.Namespace) -> str | None:
    """Return the human-readable role requiring NYMERIA_SERVICE_TOKEN, if any."""
    command = getattr(args, "command", None)
    if command == "service":
        return "the foreground gateway service"
    return _SERVICE_TOKEN_REQUIRED_COMMANDS.get(command)


def _require_service_token(settings, role: str, *, stream=None) -> str:
    """Return the admin service token or fail with provisioning guidance."""
    token = settings.nymeria_service_token
    if isinstance(token, str):
        token = token.strip()
    if token:
        return token

    stream = stream or sys.stdout
    print(f"\n[Error] NYMERIA_SERVICE_TOKEN is required for {role}.", file=stream)
    print("  Provision the bot-service admin once:", file=stream)
    print("    docker exec nymeria-api python run.py users add bot-service@localhost \\", file=stream)
    print("        --role admin --id bot-service", file=stream)
    print("  Then put the printed token into NYMERIA_SERVICE_TOKEN in .env.docker.", file=stream)
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
        transport=args.transport,
        renderer=args.renderer,
        api_url=args.api_url,
        api_key=args.api_key,
        user_id=args.user_id,
        user_id_explicit=bool(getattr(args, "user_id_explicit", False)),
        alt_screen=args.alt_screen,
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
        from nymeria import NymeriaAgent
        from nymeria.tools import ALL_TOOLS

        agent = NymeriaAgent(tools=list(ALL_TOOLS))
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

    print(f"Starting Nymeria API server on {host}:{port}...")
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
) -> str:
    """Set env overrides for slim mode and return the loopback base URL.

    Slim mode collapses the Docker stack into one process: SQLite for
    persistence, no Redis event bus, watchdog as an async task, and MCP
    mounted on the same FastAPI app. The env values are set BEFORE
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

    # If settings were loaded earlier (e.g. by `_load_environment()` callers
    # or argparse imports), reset the cache so the slim overrides take effect.
    try:
        from nymeria.config import get_settings

        get_settings.cache_clear()
    except Exception:
        pass

    return base_url


def run_slim(args: argparse.Namespace) -> None:
    """Run the single-process slim launcher (API + ticker + MCP + watchdog)."""
    host = getattr(args, "host", None) or "127.0.0.1"
    port = int(getattr(args, "port", None) or 8000)
    data_dir = getattr(args, "data_dir", None)
    enable_mcp = not getattr(args, "no_mcp", False)
    enable_watchdog = not getattr(args, "no_watchdog", False)

    base_url = _apply_slim_runtime_env(host, port, data_dir=data_dir)

    from nymeria.triggers.api import run_api as start_api
    from nymeria.config import get_settings

    settings = get_settings()

    print(f"Starting Nymeria SLIM (single-process) on {host}:{port}...")
    print("  - Mode: SQLite + in-process ticker + embedded MCP")
    print(f"  - Internal API URL: {base_url}")
    print(f"  - Data directory: {settings.data_dir}")
    if enable_mcp:
        print(f"  - MCP endpoint: {base_url}/mcp")
    else:
        print("  - MCP: disabled (--no-mcp)")
    if settings.watchdog_enabled and enable_watchdog:
        print(
            f"  - Watchdog: in-process (interval={settings.watchdog_interval_minutes}m, "
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
        enable_slim_watchdog=enable_watchdog,
    )


def run_service(args: argparse.Namespace) -> None:
    """Handle service subcommand (foreground gateway mode)."""
    run_gateway_foreground(args)


def run_init(args: argparse.Namespace) -> int:
    """Run first-time package setup."""
    from nymeria.setup_wizard import run_init as start_init

    return start_init(args)


def run_doctor(args: argparse.Namespace) -> int:
    """Run installation diagnostics."""
    from nymeria.doctor import run_doctor as start_doctor

    return start_doctor(args)


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
    from nymeria.triggers.api_client import NymeriaAPIClient

    settings = get_settings()

    # The ticker fires triggers through the API with service-token auth.
    _require_service_token(settings, "the worker (ticker)")

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
    # and triggers it dispatches — the API call carries
    # publish_autonomous_events=False so we don't get duplicates).
    if settings.redis_enabled and settings.redis_url:
        event_bus = create_event_bus(settings)
        set_event_bus(event_bus)
        print(f"  - Redis event bus: {_redis_url_for_display(settings.redis_url)}")

    # Initialize FCM if enabled (for autonomous notifications, dispatched
    # by the same notification helpers the ticker uses).
    if settings.fcm_enabled and settings.fcm_credentials_json:
        from nymeria.core.fcm import _init_firebase
        if _init_firebase(settings.fcm_credentials_json):
            print("  - FCM push notifications: enabled")

    # Wait briefly for the API container to become healthy before we
    # start firing TODOs. Without this, a TODO whose scheduled_for is
    # recovered at boot will fail with a connection refused before the
    # API has finished initializing; the ticker would then burn its
    # retry budget. Mirrors the watchdog's 5-second initial wait.
    health_url = api_url.rstrip("/") + "/health"
    deadline = time.monotonic() + 30
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
            "  - API health: unreachable after 30s — continuing anyway, the "
            "ticker will retry per-TODO until the API comes up."
        )

    client = NymeriaAPIClient(base_url=api_url, api_key=settings.nymeria_service_token)
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
    indexed = ticker.rebuild_schedule_index()
    if indexed > 0:
        print(f"  - Indexed {indexed} scheduled TODO(s)")
    recovered = ticker.recover_missed_schedules()
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
            },
        )

    write_worker_heartbeat()

    async def _close_executor() -> None:
        try:
            await executor.aclose()
        except Exception:
            pass  # Best-effort during shutdown.

    def signal_handler(signum, frame):
        print("\nShutdown signal received, stopping ticker...")
        ticker.stop()
        try:
            asyncio.run(_close_executor())
        except RuntimeError:
            # An asyncio loop may already be torn down; ignore.
            pass
        # Force exit — ThreadPoolExecutor threads are non-daemon and
        # would otherwise keep the process alive indefinitely.
        import os
        os._exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

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
    from nymeria.triggers.discord_bot import NymeriaDiscordBot
    from nymeria.triggers.api_client import NymeriaAPIClient

    settings = get_settings()

    if not settings.discord_bot_token:
        print("\n[Error] DISCORD_BOT_TOKEN is not set.")
        print("  1. Create a bot at https://discord.com/developers/applications")
        print("  2. Copy the bot token and add it to your .env file:")
        print("     DISCORD_BOT_TOKEN=your-token-here")
        sys.exit(1)

    # API URL is required — the bot is a thin client
    api_url = getattr(args, "api_url", None) or "http://nymeria-api:8000"
    # Bots authenticate as the bot-service admin and route per-user traffic
    # with X-Nymeria-Act-As.
    api_key = _require_service_token(settings, "the Discord bot")

    print("Starting Nymeria Discord Bot (thin client)...")
    print("  - Mode: gateway (WebSocket)")
    print(f"  - Respond mode: {settings.discord_respond_mode}")
    print(f"  - API: {api_url}")
    print("  - Auth: service token")

    # Create API client
    api = NymeriaAPIClient(base_url=api_url, api_key=api_key)

    # Create and run bot
    bot = NymeriaDiscordBot(
        api=api,
        respond_mode=settings.discord_respond_mode,
    )

    # Handle shutdown signals
    def signal_handler(signum, frame):
        print("\nShutdown signal received, stopping Discord bot...")
        import os
        os._exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("\nConnecting to Discord...")
    bot.run(settings.discord_bot_token, log_handler=None)


def run_watchdog(args: argparse.Namespace) -> None:
    """
    Run the watchdog worker (thin client).

    Polls the Nymeria REST API for stale TODOs and POSTs nudges to /chat
    with is_self_invoke=true. Owns no NymeriaAgent — the API handles all
    agent execution and event publishing.
    """
    import asyncio

    from nymeria.config import get_settings
    from nymeria.triggers.api_client import NymeriaAPIClient
    from nymeria.triggers.watchdog_worker import WatchdogWorker

    settings = get_settings()

    if not settings.watchdog_enabled:
        print("[Info] Watchdog is disabled (WATCHDOG_ENABLED=false). Exiting.")
        sys.exit(0)

    # Per-user act-as routing requires the admin service token.
    api_url = getattr(args, "api_url", None) or "http://nymeria-api:8000"
    api_key = _require_service_token(settings, "the watchdog worker")

    print("Starting Nymeria Watchdog (thin client)...")
    print(f"  - API: {api_url}")
    print(f"  - Interval: {settings.watchdog_interval_minutes}m")
    print(f"  - Staleness threshold: {settings.todo_staleness_minutes}m")
    print("  - Auth: service token")

    api = NymeriaAPIClient(base_url=api_url, api_key=api_key)
    worker = WatchdogWorker(client=api, settings=settings)

    def signal_handler(signum, frame):
        print("\nShutdown signal received, stopping watchdog worker...")
        worker.stop()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        asyncio.run(worker.run())
    except KeyboardInterrupt:
        pass
    print("Watchdog worker exited.")


def run_telegram_bot(args: argparse.Namespace) -> None:
    """
    Run the Telegram bot (polling mode).

    Thin client architecture: the bot calls the Nymeria REST API for
    all operations instead of running its own NymeriaAgent. This
    ensures Telegram always reflects the same state as the frontend.
    """
    from nymeria.config import get_settings
    from nymeria.triggers.telegram_bot import NymeriaTelegramBot
    from nymeria.triggers.api_client import NymeriaAPIClient

    settings = get_settings()

    if not settings.telegram_bot_token:
        print("\n[Error] TELEGRAM_BOT_TOKEN is not set.")
        print("  1. Message @BotFather on Telegram")
        print("  2. Create a new bot with /newbot")
        print("  3. Copy the token and add it to your .env:")
        print("     TELEGRAM_BOT_TOKEN=your-token-here")
        sys.exit(1)

    # API URL is required — the bot is a thin client
    api_url = getattr(args, "api_url", None) or "http://nymeria-api:8000"
    api_key = _require_service_token(settings, "the Telegram bot")

    print("Starting Nymeria Telegram Bot (thin client)...")
    print("  - Mode: polling")
    print(f"  - API: {api_url}")
    if settings.telegram_default_chat_id:
        print(f"  - Default chat: {settings.telegram_default_chat_id}")
    print("  - Auth: service token")

    # Create API client
    api = NymeriaAPIClient(base_url=api_url, api_key=api_key)

    # Create and run bot
    bot = NymeriaTelegramBot(
        api=api,
        bot_token=settings.telegram_bot_token,
        default_chat_id=settings.telegram_default_chat_id,
    )

    # Handle shutdown signals
    def signal_handler(signum, frame):
        print("\nShutdown signal received, stopping Telegram bot...")
        import os
        os._exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("\nConnecting to Telegram...")
    bot.run()


def run_slack_bot(args: argparse.Namespace) -> None:
    """
    Run the Slack bot (Socket Mode).

    Thin client architecture: the bot calls the Nymeria REST API for all
    operations instead of running its own NymeriaAgent.
    """
    from nymeria.config import get_settings
    from nymeria.triggers.api_client import NymeriaAPIClient
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

    api_url = getattr(args, "api_url", None) or "http://nymeria-api:8000"
    api_key = _require_service_token(settings, "the Slack bot")

    print("Starting Nymeria Slack Bot (thin client)...")
    print("  - Mode: Socket Mode")
    print(f"  - Respond mode: {settings.slack_respond_mode}")
    print(f"  - API: {api_url}")
    print("  - Auth: service token")

    api = NymeriaAPIClient(base_url=api_url, api_key=api_key)
    bot = NymeriaSlackBot(
        api=api,
        bot_token=settings.slack_bot_token,
        app_token=settings.slack_app_token,
        respond_mode=settings.slack_respond_mode,
        show_tool_events=settings.slack_show_tool_events,
    )

    def signal_handler(signum, frame):
        print("\nShutdown signal received, stopping Slack bot...")
        import os
        os._exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("\nConnecting to Slack...")
    bot.run()


def run_matrix_bot(args: argparse.Namespace) -> None:
    """
    Run the Matrix bot (Client-Server API sync loop).

    Thin client architecture: the bot calls the Nymeria REST API for all
    operations instead of running its own NymeriaAgent.
    """
    from nymeria.config import get_settings
    from nymeria.triggers.api_client import NymeriaAPIClient
    from nymeria.triggers.matrix_bot import NymeriaMatrixBot

    settings = get_settings()

    if not settings.matrix_homeserver:
        print("\n[Error] MATRIX_HOMESERVER is not set.")
        print("  Example: MATRIX_HOMESERVER=https://matrix.example.org")
        sys.exit(1)
    if not settings.matrix_access_token and not (
        settings.matrix_user_id and settings.matrix_password
    ):
        print("\n[Error] Matrix authentication is not configured.")
        print("  Set MATRIX_ACCESS_TOKEN, or MATRIX_USER_ID + MATRIX_PASSWORD.")
        sys.exit(1)

    api_url = getattr(args, "api_url", None) or "http://nymeria-api:8000"
    api_key = _require_service_token(settings, "the Matrix bot")
    free_response_rooms = [
        room.strip()
        for room in (settings.matrix_free_response_rooms or "").split(",")
        if room.strip()
    ]

    print("Starting Nymeria Matrix Bot (thin client)...")
    print("  - Mode: Client-Server API sync")
    print(f"  - Homeserver: {settings.matrix_homeserver}")
    print(f"  - Respond mode: {settings.matrix_respond_mode}")
    print(f"  - Auto-join invites: {settings.matrix_auto_join}")
    print(f"  - API: {api_url}")
    print("  - Auth: service token")

    api = NymeriaAPIClient(base_url=api_url, api_key=api_key)
    bot = NymeriaMatrixBot(
        api=api,
        homeserver=settings.matrix_homeserver,
        access_token=settings.matrix_access_token,
        user_id=settings.matrix_user_id,
        password=settings.matrix_password,
        device_id=settings.matrix_device_id,
        respond_mode=settings.matrix_respond_mode,
        free_response_rooms=free_response_rooms,
        auto_join=settings.matrix_auto_join,
    )

    def signal_handler(signum, frame):
        print("\nShutdown signal received, stopping Matrix bot...")
        import os
        os._exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("\nConnecting to Matrix...")
    bot.run()


def run_mattermost_bot(args: argparse.Namespace) -> None:
    """
    Run the Mattermost bot (WebSocket mode).

    Thin client architecture: the bot calls the Nymeria REST API for all
    operations instead of running its own NymeriaAgent.
    """
    from nymeria.config import get_settings
    from nymeria.triggers.api_client import NymeriaAPIClient
    from nymeria.triggers.mattermost_bot import NymeriaMattermostBot

    settings = get_settings()

    if not settings.mattermost_base_url:
        print("\n[Error] MATTERMOST_BASE_URL is not set.")
        print("  Example: MATTERMOST_BASE_URL=https://mattermost.example.com")
        sys.exit(1)
    if not settings.mattermost_access_token:
        print("\n[Error] MATTERMOST_ACCESS_TOKEN is not set.")
        print("  Create a Mattermost bot account and copy its generated access token.")
        sys.exit(1)

    api_url = getattr(args, "api_url", None) or "http://nymeria-api:8000"
    api_key = _require_service_token(settings, "the Mattermost bot")

    print("Starting Nymeria Mattermost Bot (thin client)...")
    print("  - Mode: WebSocket")
    print(f"  - Server: {settings.mattermost_base_url}")
    print(f"  - Respond mode: {settings.mattermost_respond_mode}")
    print(f"  - API: {api_url}")
    print("  - Auth: service token")

    api = NymeriaAPIClient(base_url=api_url, api_key=api_key)
    bot = NymeriaMattermostBot(
        api=api,
        base_url=settings.mattermost_base_url,
        access_token=settings.mattermost_access_token,
        respond_mode=settings.mattermost_respond_mode,
        show_tool_events=settings.mattermost_show_tool_events,
    )

    def signal_handler(signum, frame):
        print("\nShutdown signal received, stopping Mattermost bot...")
        import os
        os._exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("\nConnecting to Mattermost...")
    bot.run()


def run_zulip_bot(args: argparse.Namespace) -> None:
    """
    Run the Zulip bot (event queue mode).

    Thin client architecture: the bot calls the Nymeria REST API for all
    operations instead of running its own NymeriaAgent.
    """
    from nymeria.config import get_settings
    from nymeria.triggers.api_client import NymeriaAPIClient
    from nymeria.triggers.zulip_bot import NymeriaZulipBot

    settings = get_settings()

    if not settings.zulip_base_url:
        print("\n[Error] ZULIP_BASE_URL is not set.")
        print("  Example: ZULIP_BASE_URL=https://your-org.zulipchat.com")
        sys.exit(1)
    if not settings.zulip_email:
        print("\n[Error] ZULIP_EMAIL is not set.")
        print("  Set it to the Zulip bot email address.")
        sys.exit(1)
    if not settings.zulip_api_key:
        print("\n[Error] ZULIP_API_KEY is not set.")
        print("  Copy the bot API key from Zulip Personal settings > Bots.")
        sys.exit(1)

    api_url = getattr(args, "api_url", None) or "http://nymeria-api:8000"
    api_key = _require_service_token(settings, "the Zulip bot")

    print("Starting Nymeria Zulip Bot (thin client)...")
    print("  - Mode: Events API long-poll")
    print(f"  - Realm: {settings.zulip_base_url}")
    print(f"  - Respond mode: {settings.zulip_respond_mode}")
    print(f"  - API: {api_url}")
    print("  - Auth: service token")

    api = NymeriaAPIClient(base_url=api_url, api_key=api_key)
    bot = NymeriaZulipBot(
        api=api,
        base_url=settings.zulip_base_url,
        email=settings.zulip_email,
        api_key=settings.zulip_api_key,
        respond_mode=settings.zulip_respond_mode,
        show_tool_events=settings.zulip_show_tool_events,
    )

    def signal_handler(signum, frame):
        print("\nShutdown signal received, stopping Zulip bot...")
        import os
        os._exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("\nConnecting to Zulip...")
    bot.run()


def run_rocketchat_bot(args: argparse.Namespace) -> None:
    """
    Run the Rocket.Chat bot (realtime mode).

    Thin client architecture: the bot calls the Nymeria REST API for all
    operations instead of running its own NymeriaAgent.
    """
    from nymeria.config import get_settings
    from nymeria.triggers.api_client import NymeriaAPIClient
    from nymeria.triggers.rocketchat_bot import NymeriaRocketChatBot

    settings = get_settings()

    if not settings.rocketchat_base_url:
        print("\n[Error] ROCKETCHAT_BASE_URL is not set.")
        print("  Example: ROCKETCHAT_BASE_URL=https://chat.example.com")
        sys.exit(1)
    if not settings.rocketchat_user_id:
        print("\n[Error] ROCKETCHAT_USER_ID is not set.")
        print("  Use the user ID shown when generating the Rocket.Chat personal access token.")
        sys.exit(1)
    if not settings.rocketchat_auth_token:
        print("\n[Error] ROCKETCHAT_AUTH_TOKEN is not set.")
        print("  Create a Rocket.Chat bot/user personal access token and save it here.")
        sys.exit(1)

    api_url = getattr(args, "api_url", None) or "http://nymeria-api:8000"
    api_key = _require_service_token(settings, "the Rocket.Chat bot")

    print("Starting Nymeria Rocket.Chat Bot (thin client)...")
    print("  - Mode: realtime room stream")
    print(f"  - Server: {settings.rocketchat_base_url}")
    print(f"  - Respond mode: {settings.rocketchat_respond_mode}")
    print(f"  - API: {api_url}")
    print("  - Auth: service token")

    api = NymeriaAPIClient(base_url=api_url, api_key=api_key)
    bot = NymeriaRocketChatBot(
        api=api,
        base_url=settings.rocketchat_base_url,
        auth_token=settings.rocketchat_auth_token,
        user_id=settings.rocketchat_user_id,
        respond_mode=settings.rocketchat_respond_mode,
        show_tool_events=settings.rocketchat_show_tool_events,
    )

    def signal_handler(signum, frame):
        print("\nShutdown signal received, stopping Rocket.Chat bot...")
        import os
        os._exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("\nConnecting to Rocket.Chat...")
    bot.run()


def run_signal_bot(args: argparse.Namespace) -> None:
    """
    Run the Signal bot via signal-cli-rest-api SSE/JSON-RPC mode.

    Thin client architecture: the bot calls the Nymeria REST API for all
    operations instead of running its own NymeriaAgent.
    """
    from nymeria.config import get_settings
    from nymeria.triggers.api_client import NymeriaAPIClient
    from nymeria.triggers.signal_bot import create_signal_bot_from_settings

    settings = get_settings()

    if not settings.signal_http_url:
        print("\n[Error] SIGNAL_HTTP_URL is not set.")
        print("  Example: SIGNAL_HTTP_URL=http://signal-cli-rest-api:8080")
        sys.exit(1)
    if not settings.signal_account:
        print("\n[Error] SIGNAL_ACCOUNT is not set.")
        print("  Example: SIGNAL_ACCOUNT=+15551234567")
        sys.exit(1)

    api_url = getattr(args, "api_url", None) or "http://nymeria-api:8000"
    api_key = _require_service_token(settings, "the Signal bot")

    print("Starting Nymeria Signal Bot (thin client)...")
    print("  - Mode: signal-cli-rest-api SSE/JSON-RPC")
    print(f"  - Signal daemon: {settings.signal_http_url}")
    print(f"  - Account: {settings.signal_account}")
    print(f"  - Respond mode: {settings.signal_respond_mode}")
    print(f"  - API: {api_url}")
    print("  - Auth: service token")

    api = NymeriaAPIClient(base_url=api_url, api_key=api_key)
    bot = create_signal_bot_from_settings(api, settings=settings)

    def signal_handler(signum, frame):
        print("\nShutdown signal received, stopping Signal bot...")
        import os
        os._exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("\nConnecting to Signal...")
    bot.run()


def run_twitch_bot(args: argparse.Namespace) -> None:
    """
    Run the Twitch bot.

    Connects to a Twitch channel via TwitchIO v3 and responds to !commands.
    Chat messages are buffered in memory and optionally evaluated periodically
    ("pulse"). Moderation tools are available via the thread's tool config.
    """
    from nymeria import NymeriaAgent
    from nymeria.tools import ALL_TOOLS
    from nymeria.config import get_settings
    from nymeria.triggers.twitch_bot import NymeriaTwitchBot
    from nymeria.core.twitch_runtime import register_twitch_bot

    settings = get_settings()

    if not settings.twitch_client_id or not settings.twitch_channel:
        print("\n[Error] Twitch credentials not configured.")
        print("  Required environment variables:")
        print("    TWITCH_CLIENT_ID     — from Twitch Developer Console")
        print("    TWITCH_CLIENT_SECRET — from Twitch Developer Console")
        print("    TWITCH_BOT_USER_ID   — numeric ID of the bot's Twitch account")
        print("    TWITCH_CHANNEL       — channel to join")
        print("\n  1. Create an app at https://dev.twitch.tv/console/apps")
        print("  2. Add credentials to .env or .env.docker")
        print("  3. Run again: python run.py twitch-bot")
        sys.exit(1)

    # The Twitch bot's agent invokes tools that call back into the API.
    _require_service_token(settings, "the Twitch bot")

    print("Starting Nymeria Twitch Bot...")
    print(f"  - Channel: #{settings.twitch_channel}")
    print(f"  - Buffer size: {settings.twitch_buffer_size}")
    print(f"  - Pulse: {'enabled' if settings.twitch_pulse_enabled else 'disabled'}")
    print(f"  - Model: {settings.llm_model}")

    # Initialize Redis event bus if configured
    if settings.redis_enabled and settings.redis_url:
        from nymeria.core.event_bus import create_event_bus, set_event_bus
        event_bus = create_event_bus(settings)
        set_event_bus(event_bus)
        print(f"  - Redis event bus: {_redis_url_for_display(settings.redis_url)}")

    # Create agent without ticker (ticker runs in worker/api, not bot)
    agent = NymeriaAgent(tools=list(ALL_TOOLS), enable_ticker=False)
    agent.sync_agent_tools()

    # Create bot
    bot = NymeriaTwitchBot(
        agent=agent,
        client_id=settings.twitch_client_id,
        client_secret=settings.twitch_client_secret,
        bot_user_id=settings.twitch_bot_user_id,
        access_token=settings.twitch_bot_access_token,
        refresh_token=settings.twitch_bot_refresh_token,
        broadcaster_token=settings.twitch_broadcaster_token,
        broadcaster_refresh_token=settings.twitch_broadcaster_refresh_token,
        channel=settings.twitch_channel,
        buffer_size=settings.twitch_buffer_size,
        pulse_enabled=settings.twitch_pulse_enabled,
        pulse_interval=settings.twitch_pulse_interval,
        pulse_min_messages=settings.twitch_pulse_min_messages,
        command_context_count=settings.twitch_command_context_count,
        system_prompt=settings.twitch_system_prompt,
    )

    # Register the bot runtime for Twitch tools. The runtime module is outside
    # nymeria.tools so tool hot-reload does not drop this registration.
    register_twitch_bot(bot)

    # Handle shutdown signals
    def signal_handler(signum, frame):
        print("\nShutdown signal received, stopping Twitch bot...")
        import os
        os._exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("\nConnecting to Twitch...")
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

    # Handle Ctrl+C gracefully
    def signal_handler(signum, frame):
        print("\nShutdown signal received...")
        gateway.stop()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start the gateway
    gateway.start()

    # Wait for shutdown
    gateway.wait_for_stop()

    print("Gateway stopped")


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level launcher parser."""
    from nymeria.onboarding import (
        HostingOption,
        NextAction,
        ProviderAuthMethod,
        SetupStyle,
        choice_values,
    )

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
    cli_parser.add_argument(
        "--transport",
        choices=("api", "local", "auto"),
        default="api",
        help=(
            "Transport mode for CLI chat and command requests "
            "(default: api; use --transport local for embedded agent mode)"
        ),
    )
    cli_parser.add_argument(
        "--renderer",
        choices=("full", "rich", "plain", "auto"),
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
        "--no-alt-screen",
        dest="alt_screen",
        action="store_false",
        default=True,
        help="Disable alternate-screen mode for full-screen renderer",
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
        help="Start single-process local launcher (API + ticker + MCP + watchdog)",
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
        default=8000,
        help="Port to listen on (default: 8000)",
    )
    slim_parser.add_argument(
        "--data-dir",
        default=None,
        help="Runtime data directory (writes NYMERIA_DATA_DIR before settings load)",
    )
    slim_parser.add_argument(
        "--no-mcp",
        action="store_true",
        help="Skip mounting the embedded MCP server at /mcp",
    )
    slim_parser.add_argument(
        "--no-watchdog",
        action="store_true",
        help="Skip the in-process watchdog task even when WATCHDOG_ENABLED=true",
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
    init_parser.add_argument(
        "--provider",
        choices=("anthropic", "openai", "openrouter"),
        default=None,
    )
    init_parser.add_argument(
        "--model",
        default=None,
        help="Model identifier to write to config.env",
    )
    init_parser.add_argument("--api-key", default=None)
    init_parser.add_argument(
        "--hosting",
        choices=choice_values(HostingOption),
        default=None,
        help="Onboarding hosting profile",
    )
    init_parser.add_argument(
        "--auth-method",
        choices=choice_values(ProviderAuthMethod),
        default=None,
        help="Provider authentication method",
    )
    init_parser.add_argument(
        "--setup-style",
        choices=choice_values(SetupStyle),
        default=None,
        help="Amount of setup detail to collect",
    )
    init_parser.add_argument(
        "--next-action",
        choices=choice_values(NextAction),
        default=None,
        help="Post-setup handoff action",
    )
    init_parser.add_argument(
        "--cliproxy-root",
        default=None,
        help="CLIProxy temp/latest directory for OAuth setup",
    )
    init_parser.add_argument(
        "--cliproxy-base-url",
        default=None,
        help="Host-reachable CLIProxy root URL for Claude OAuth setup",
    )
    init_parser.add_argument(
        "--embedding-api-key",
        default=None,
        help="Optional OpenAI-compatible key for RAG/memory/skill embeddings",
    )
    init_parser.add_argument(
        "--openai-api-key",
        default=None,
        help="Optional OpenAI key for image generation/STT/OpenAI-backed tools",
    )
    init_parser.add_argument(
        "--gemini-api-key",
        default=None,
        help="Optional Gemini key for Gemini image/document/TTS tools",
    )
    init_parser.add_argument(
        "--perplexity-api-key",
        default=None,
        help="Optional Perplexity key for web search",
    )
    init_parser.add_argument(
        "--root",
        default=None,
        help="Runtime root for config.env and the default data/ directory",
    )
    init_parser.add_argument(
        "--data-dir",
        default=None,
        help="Advanced setup data directory to write as NYMERIA_DATA_DIR",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite config.env if it exists",
    )
    init_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Require flags instead of prompting",
    )
    init_parser.add_argument(
        "--skip-llm-test",
        action="store_true",
        help="Write config without making the provider smoke-test API call",
    )
    init_parser.add_argument(
        "--run-doctor",
        action="store_true",
        help="Run nymeria doctor after writing config",
    )
    init_parser.add_argument(
        "--full-doctor",
        action="store_true",
        help="Run post-init doctor with its live LLM check; implies --run-doctor",
    )

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

    # Discord bot subcommand
    discord_parser = subparsers.add_parser(
        "discord-bot",
        help="Start Discord bot (gateway mode)"
    )
    discord_parser.add_argument(
        "--api-url",
        default=None,
        help="URL of running Nymeria API (e.g. http://localhost:8000). "
             "Enables autonomous task results to appear in Discord channels "
             "when running the bot and API as separate local processes.",
    )

    # Telegram bot subcommand
    telegram_parser = subparsers.add_parser(
        "telegram-bot",
        help="Start Telegram bot (polling mode)"
    )
    telegram_parser.add_argument(
        "--api-url",
        default=None,
        help="URL of running Nymeria API (e.g. http://localhost:8000)",
    )

    # Slack bot subcommand
    slack_parser = subparsers.add_parser(
        "slack-bot",
        help="Start Slack bot (Socket Mode)",
    )
    slack_parser.add_argument(
        "--api-url",
        default=None,
        help="URL of running Nymeria API (e.g. http://localhost:8000)",
    )

    # Matrix bot subcommand
    matrix_parser = subparsers.add_parser(
        "matrix-bot",
        help="Start Matrix bot (sync loop)",
    )
    matrix_parser.add_argument(
        "--api-url",
        default=None,
        help="URL of running Nymeria API (e.g. http://localhost:8000)",
    )

    # Mattermost bot subcommand
    mattermost_parser = subparsers.add_parser(
        "mattermost-bot",
        help="Start Mattermost bot (WebSocket mode)",
    )
    mattermost_parser.add_argument(
        "--api-url",
        default=None,
        help="URL of running Nymeria API (e.g. http://localhost:8000)",
    )

    # Zulip bot subcommand
    zulip_parser = subparsers.add_parser(
        "zulip-bot",
        help="Start Zulip bot (event queue mode)",
    )
    zulip_parser.add_argument(
        "--api-url",
        default=None,
        help="URL of running Nymeria API (e.g. http://localhost:8000)",
    )

    # Rocket.Chat bot subcommand
    rocketchat_parser = subparsers.add_parser(
        "rocketchat-bot",
        help="Start Rocket.Chat bot (realtime mode)",
    )
    rocketchat_parser.add_argument(
        "--api-url",
        default=None,
        help="URL of running Nymeria API (e.g. http://localhost:8000)",
    )

    # Signal bot subcommand
    signal_parser = subparsers.add_parser(
        "signal-bot",
        help="Start Signal bot (signal-cli SSE mode)",
    )
    signal_parser.add_argument(
        "--api-url",
        default=None,
        help="URL of running Nymeria API (e.g. http://localhost:8000)",
    )

    # Watchdog subcommand (thin client)
    watchdog_parser = subparsers.add_parser(
        "watchdog",
        help="Start watchdog worker (thin client — polls API for stale TODOs)"
    )
    watchdog_parser.add_argument(
        "--api-url",
        default=None,
        help="URL of running Nymeria API (e.g. http://localhost:8000). "
             "Defaults to http://nymeria-api:8000 for Docker deployments.",
    )

    # Twitch bot subcommand
    subparsers.add_parser(
        "twitch-bot",
        help="Start Twitch chat bot"
    )

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

    # Service subcommand (foreground gateway)
    subparsers.add_parser(
        "service",
        help="Run gateway server in foreground",
        description="Start the GatewayServer (REST transport) in the foreground with graceful Ctrl+C shutdown.",
    )

    # Users subcommand (account provisioning)
    from nymeria.cli import users as users_cli
    users_cli.build_parser(subparsers)

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
            port=int(getattr(args, "port", None) or 8000),
            data_dir=getattr(args, "data_dir", None),
        )

    # Setup logging (except for service commands and STDIO MCP, which must keep
    # stdout reserved for JSON-RPC messages. The MCP server configures stderr
    # logging internally so client transports are not corrupted.
    if args.command not in ("service", "mcp", "init", "doctor"):
        setup_logging(args.log_level)

    if service_token_required:
        from nymeria.config import get_settings
        _require_launch_mode_service_token(args, get_settings())

    # Slim bootstraps its own internal service token, so the generic
    # "NYMERIA_SERVICE_TOKEN not set" warning is misleading there.
    suppress_service_token_warning = service_token_required or args.command == "slim"

    # Validate configuration before running commands that need it
    # Skip validation for service status checks and help
    if args.command == "cli":
        if getattr(args, "transport", "api") == "local":
            validate_config(suppress_service_token_warning=suppress_service_token_warning)
    elif args.command in ("api", "slim", "mcp", "worker", "discord-bot", "telegram-bot", "slack-bot", "matrix-bot", "mattermost-bot", "zulip-bot", "rocketchat-bot", "signal-bot", "twitch-bot", "watchdog", "service"):
        validate_config(suppress_service_token_warning=suppress_service_token_warning)
    elif args.command == "users":
        # Account CLI operates on the local DB directly; skip NYMERIA_API_KEY
        # check so the admin can provision users before the API is configured.
        validate_config(skip_api_key=True)

    # Run appropriate command
    if args.command == "cli":
        run_cli(args)
    elif args.command == "api":
        run_api(args)
    elif args.command == "slim":
        run_slim(args)
    elif args.command == "worker":
        run_worker(args)
    elif args.command == "init":
        sys.exit(run_init(args))
    elif args.command == "doctor":
        sys.exit(run_doctor(args))
    elif args.command == "discord-bot":
        run_discord_bot(args)
    elif args.command == "telegram-bot":
        run_telegram_bot(args)
    elif args.command == "slack-bot":
        run_slack_bot(args)
    elif args.command == "matrix-bot":
        run_matrix_bot(args)
    elif args.command == "mattermost-bot":
        run_mattermost_bot(args)
    elif args.command == "zulip-bot":
        run_zulip_bot(args)
    elif args.command == "rocketchat-bot":
        run_rocketchat_bot(args)
    elif args.command == "signal-bot":
        run_signal_bot(args)
    elif args.command == "watchdog":
        run_watchdog(args)
    elif args.command == "twitch-bot":
        run_twitch_bot(args)
    elif args.command == "mcp":
        run_mcp(args)
    elif args.command == "service":
        run_service(args)
    elif args.command == "users":
        from nymeria.cli import users as users_cli

        sys.exit(users_cli.dispatch(args))
    elif args.command == "completion":
        run_completion(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
