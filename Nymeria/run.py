#!/usr/bin/env python
"""
Nymeria - Entry point script.

Usage:
    python run.py cli          # Start CLI interface
    python run.py api          # Start REST API server
    python run.py api --port 8080  # Start API on custom port
    python run.py worker           # Start worker (ticker only, for Docker)
    python run.py discord-bot     # Start Discord bot (gateway mode)
    python run.py telegram-bot    # Start Telegram bot (polling mode)
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
from logging.handlers import RotatingFileHandler
from pathlib import Path

import os

# Add project to path — for PyInstaller frozen builds, the bundled modules are
# already on sys.path, but we still need the project root for .env resolution.
if getattr(sys, "frozen", False):
    _project_root = Path(os.environ.get("NYMERIA_PROJECT_ROOT", Path(sys.executable).resolve().parent))
else:
    _project_root = Path(__file__).resolve().parent
sys.path.insert(0, str(_project_root))

from dotenv import load_dotenv


def _load_environment() -> None:
    """Load environment files relative to project root, overriding inherited values."""
    project_root = _project_root

    # Load base config first, then docker overrides if present.
    # override=True ensures restarts pick up latest .env values even when
    # the parent process has stale exported environment variables.
    load_dotenv(project_root / ".env", override=True)
    load_dotenv(project_root / ".env.docker", override=True)


# Load environment variables before importing settings/users of os.environ
_load_environment()


_SERVICE_TOKEN_REQUIRED_COMMANDS = {
    "worker": "the worker (ticker)",
    "discord-bot": "the Discord bot",
    "telegram-bot": "the Telegram bot",
    "watchdog": "the watchdog worker",
    "twitch-bot": "the Twitch bot",
    "mcp": "the MCP thin client",
}


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
        print("  1. Copy .env.minimal to .env (or use .env.example for all options)")
        print("  2. Generate API key: python -c \"import secrets; print(secrets.token_urlsafe(32))\"")
        print("  3. Add your LLM provider API key")
        print("  4. Run again: python run.py api")
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


def run_cli(args: argparse.Namespace) -> None:
    """Run the CLI interface."""
    # Suppress logging for clean CLI experience — errors like missing API keys
    # (e.g. RAG embedding) are expected in local dev and shouldn't clutter the REPL.
    # Fatal issues still surface via stream error events rendered by StreamRenderer.
    logging.getLogger("nymeria").setLevel(logging.CRITICAL)

    from nymeria import NymeriaAgent
    from nymeria.tools import get_all_tools_with_agents
    from nymeria.triggers.cli import run_cli as start_cli

    # Create agent with all tools
    agent = NymeriaAgent(tools=get_all_tools_with_agents())
    agent.sync_agent_tools()

    # Start CLI
    start_cli(agent=agent, thread_id=args.thread)


def run_api(args: argparse.Namespace) -> None:
    """Run the REST API server."""
    from nymeria.triggers.api import run_api as start_api
    from nymeria.config import get_settings

    settings = get_settings()
    host = args.host or settings.api_host
    port = args.port or settings.api_port

    print(f"Starting Nymeria API server on {host}:{port}...")
    print(f"  - Docs: http://{host}:{port}/docs")
    print(f"  - ReDoc: http://{host}:{port}/redoc")
    if settings.redis_enabled and settings.redis_url:
        print(f"  - Redis event bus: {settings.redis_url}")

    # Agent creation, Redis event bus, FCM, ticker-disable logic, and tool
    # sync are all handled by create_api_app() inside start_api().
    start_api(host=host, port=port)


def run_service(args: argparse.Namespace) -> None:
    """Handle service subcommand (foreground gateway mode)."""
    run_gateway_foreground(args)


def run_worker(args: argparse.Namespace) -> None:
    """
    Run the worker (ticker only, no API server).

    This is designed for Docker deployments where the API and worker
    run in separate containers, sharing state via PostgreSQL and Redis.
    """
    from nymeria import NymeriaAgent
    from nymeria.tools import get_all_tools_with_agents
    from nymeria.config import get_settings
    from nymeria.core.event_bus import create_event_bus, set_event_bus

    settings = get_settings()

    # The ticker fires triggers through the API with service-token auth.
    _require_service_token(settings, "the worker (ticker)")

    print("Starting Nymeria Worker (ticker mode)...")
    print(f"  - Ticker poll interval: {settings.ticker_poll_interval}s")
    print(f"  - Watchdog enabled: {settings.watchdog_enabled}")
    print(f"  - Redis enabled: {settings.redis_enabled}")
    print(f"  - Data directory: {settings.data_dir}")

    # Initialize Redis event bus if configured
    if settings.redis_enabled and settings.redis_url:
        event_bus = create_event_bus(settings)
        set_event_bus(event_bus)
        print(f"  - Redis event bus: {settings.redis_url}")

    # Initialize FCM if enabled
    if settings.fcm_enabled and settings.fcm_credentials_json:
        from nymeria.core.fcm import _init_firebase
        if _init_firebase(settings.fcm_credentials_json):
            print(f"  - FCM push notifications: enabled")

    # Create agent with all tools (this starts the ticker)
    agent = NymeriaAgent(tools=get_all_tools_with_agents())

    # Sync callable thread tools into the registry
    agent.sync_agent_tools()

    # Handle shutdown signals
    def signal_handler(signum, frame):
        print("\nShutdown signal received, stopping ticker...")
        if agent._ticker:
            agent._ticker.stop()
        # Force exit — ThreadPoolExecutor threads are non-daemon and
        # would otherwise keep the process alive indefinitely.
        import os
        os._exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("\nWorker running. Press Ctrl+C to stop.")

    # Keep the process alive
    try:
        while True:
            import time
            time.sleep(1)
    except KeyboardInterrupt:
        if agent._ticker:
            agent._ticker.stop()
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
    print(f"  - Mode: gateway (WebSocket)")
    print(f"  - Respond mode: {settings.discord_respond_mode}")
    print(f"  - API: {api_url}")
    print(f"  - Auth: service token")

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
    print(f"  - Auth: service token")

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
    print(f"  - Mode: polling")
    print(f"  - API: {api_url}")
    if settings.telegram_default_chat_id:
        print(f"  - Default chat: {settings.telegram_default_chat_id}")
    print(f"  - Auth: service token")

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


def run_twitch_bot(args: argparse.Namespace) -> None:
    """
    Run the Twitch bot.

    Connects to a Twitch channel via TwitchIO v3 and responds to !commands.
    Chat messages are buffered in memory and optionally evaluated periodically
    ("pulse"). Moderation tools are available via the thread's tool config.
    """
    from nymeria import NymeriaAgent
    from nymeria.tools import get_all_tools_with_agents
    from nymeria.config import get_settings
    from nymeria.triggers.twitch_bot import NymeriaTwitchBot
    from nymeria.core.twitch_runtime import register_twitch_bot

    settings = get_settings()

    if not settings.twitch_client_id:
        print("\n[Error] Twitch credentials not configured.")
        print("  Required environment variables:")
        print("    TWITCH_CLIENT_ID     — from Twitch Developer Console")
        print("    TWITCH_CLIENT_SECRET — from Twitch Developer Console")
        print("    TWITCH_BOT_USER_ID   — numeric ID of the bot's Twitch account")
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
        print(f"  - Redis event bus: {settings.redis_url}")

    # Create agent without ticker (ticker runs in worker/api, not bot)
    agent = NymeriaAgent(tools=get_all_tools_with_agents(), enable_ticker=False)
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

    print(f"Starting Nymeria Gateway in foreground mode...")
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


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Nymeria - Personal AI Assistant",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python run.py cli                # Start CLI interface
    python run.py cli -t mythread    # Start CLI with specific thread ID
    python run.py api                # Start API server (default port 8000)
    python run.py api -p 8080        # Start API on port 8080
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
        "--thread",
        "-t",
        default=None,
        help="Thread ID for conversation persistence",
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

    # Worker subcommand
    subparsers.add_parser(
        "worker",
        help="Start worker (ticker only, for Docker deployments)"
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

    args = parser.parse_args()
    service_token_required = _service_token_requirement(args) is not None

    # Setup logging (except for service commands and STDIO MCP, which must keep
    # stdout reserved for JSON-RPC messages. The MCP server configures stderr
    # logging internally so client transports are not corrupted.
    if args.command not in ("service", "mcp"):
        setup_logging(args.log_level)

    if service_token_required:
        from nymeria.config import get_settings
        _require_launch_mode_service_token(args, get_settings())

    # Validate configuration before running commands that need it
    # Skip validation for service status checks and help
    if args.command in ("cli", "api", "mcp", "worker", "discord-bot", "telegram-bot", "twitch-bot", "watchdog", "service"):
        validate_config(suppress_service_token_warning=service_token_required)
    elif args.command == "users":
        # Account CLI operates on the local DB directly; skip NYMERIA_API_KEY
        # check so the admin can provision users before the API is configured.
        validate_config(skip_api_key=True)

    # Run appropriate command
    if args.command == "cli":
        run_cli(args)
    elif args.command == "api":
        run_api(args)
    elif args.command == "worker":
        run_worker(args)
    elif args.command == "discord-bot":
        run_discord_bot(args)
    elif args.command == "telegram-bot":
        run_telegram_bot(args)
    elif args.command == "watchdog":
        run_watchdog(args)
    elif args.command == "twitch-bot":
        run_twitch_bot(args)
    elif args.command == "mcp":
        run_mcp(args)
    elif args.command == "service":
        run_service(args)
    elif args.command == "users":
        sys.exit(users_cli.dispatch(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
