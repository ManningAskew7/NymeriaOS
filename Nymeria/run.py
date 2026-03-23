#!/usr/bin/env python
"""
Nymeria - Entry point script.

Usage:
    python run.py cli          # Start CLI interface
    python run.py api          # Start REST API server
    python run.py api --port 8080  # Start API on custom port
    python run.py worker           # Start worker (ticker only, for Docker)
    python run.py discord-bot     # Start Discord bot (gateway mode)
    python run.py mcp              # Start MCP server (stdio mode)
    python run.py mcp --http       # Start MCP server (HTTP mode)
    python run.py mcp --port 8001  # MCP HTTP mode on custom port
    python run.py service install  # Install as Windows service
    python run.py service start    # Start the Windows service
    python run.py service stop     # Stop the Windows service
    python run.py service status   # Check service status
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


def validate_config(skip_api_key: bool = False) -> None:
    """
    Validate configuration before starting any command.

    Args:
        skip_api_key: If True, skip NYMERIA_API_KEY check (for service status checks)

    Exits with code 1 if critical errors are found.
    """
    from nymeria.config import get_settings

    settings = get_settings()
    errors, warnings = settings.validate()

    # Filter out API key error if skip_api_key is True
    if skip_api_key:
        errors = [e for e in errors if "NYMERIA_API_KEY" not in e]

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
        file_mode: If True, ONLY log to file (for Windows service mode)
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
    from nymeria import NymeriaAgent
    from nymeria.tools import get_all_tools_with_agents
    from nymeria.triggers.api import run_api as start_api
    from nymeria.config import get_settings

    settings = get_settings()
    host = args.host or settings.api_host
    port = args.port or settings.api_port

    print(f"Starting Nymeria API server on {host}:{port}...")
    print(f"  - Docs: http://{host}:{port}/docs")
    print(f"  - ReDoc: http://{host}:{port}/redoc")

    # Initialize Redis event bus if configured (needed to receive worker events via SSE)
    disable_ticker = settings.redis_enabled and bool(settings.redis_url)
    if disable_ticker:
        from nymeria.core.event_bus import create_event_bus, set_event_bus
        event_bus = create_event_bus(settings)
        set_event_bus(event_bus)
        print(f"  - Redis event bus: {settings.redis_url}")

    # Create agent with all tools (core + sub-agent tools)
    # When Redis is enabled (Docker), a separate worker container runs the ticker.
    # Disable ticker in the API to prevent duplicate task execution.
    agent = NymeriaAgent(tools=get_all_tools_with_agents(), enable_ticker=not disable_ticker)

    # Start API server
    start_api(host=host, port=port, agent=agent)


def run_service(args: argparse.Namespace) -> None:
    """Handle service subcommand."""
    from nymeria.config import get_settings
    from nymeria.gateway.service import (
        get_service_status,
        install_service,
        start_service,
        stop_service,
        uninstall_service,
    )

    settings = get_settings()
    action = args.action

    if action == "install":
        print(f"Installing Nymeria as Windows service: {settings.service_name}")
        auto_start = settings.service_auto_start
        if install_service(auto_start=auto_start):
            print(f"Service '{settings.service_name}' installed successfully")
            if auto_start:
                print("  Auto-start: Enabled (will start on system boot)")
            print("  Use 'python run.py service start' to start the service")
        else:
            print("Failed to install service. Run as Administrator.")
            sys.exit(1)

    elif action == "uninstall":
        print(f"Uninstalling Windows service: {settings.service_name}")
        if uninstall_service():
            print(f"Service '{settings.service_name}' uninstalled successfully")
        else:
            print("Failed to uninstall service. Run as Administrator.")
            sys.exit(1)

    elif action == "start":
        print(f"Starting service: {settings.service_name}")
        if start_service():
            print(f"Service '{settings.service_name}' started")
        else:
            print("Failed to start service. Check if installed and run as Administrator.")
            sys.exit(1)

    elif action == "stop":
        print(f"Stopping service: {settings.service_name}")
        if stop_service():
            print(f"Service '{settings.service_name}' stopped")
        else:
            print("Failed to stop service. Check if running and run as Administrator.")
            sys.exit(1)

    elif action == "status":
        status = get_service_status()
        if status is None:
            print("Error checking service status")
            sys.exit(1)
        elif status == "not_installed":
            print(f"Service '{settings.service_name}' is not installed")
        else:
            print(f"Service '{settings.service_name}' is {status}")

    elif action == "run":
        # Run gateway in foreground (debug mode)
        run_gateway_foreground(args)

    else:
        print(f"Unknown action: {action}")
        sys.exit(1)


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

    Connects to Discord using the bot token and responds to messages
    and slash commands. Autonomous task results are posted back to
    originating Discord channels via the event bus.
    """
    from nymeria import NymeriaAgent
    from nymeria.tools import get_all_tools_with_agents
    from nymeria.config import get_settings
    from nymeria.triggers.discord_bot import NymeriaDiscordBot

    settings = get_settings()

    if not settings.discord_bot_token:
        print("\n[Error] DISCORD_BOT_TOKEN is not set.")
        print("  1. Create a bot at https://discord.com/developers/applications")
        print("  2. Copy the bot token and add it to your .env file:")
        print("     DISCORD_BOT_TOKEN=your-token-here")
        sys.exit(1)

    api_url = getattr(args, "api_url", None)

    print("Starting Nymeria Discord Bot...")
    print(f"  - Mode: gateway (WebSocket)")
    print(f"  - Respond mode: {settings.discord_respond_mode}")
    print(f"  - Model: {settings.llm_model}")

    # Initialize Redis event bus if configured
    if settings.redis_enabled and settings.redis_url:
        from nymeria.core.event_bus import create_event_bus, set_event_bus
        event_bus = create_event_bus(settings)
        set_event_bus(event_bus)
        print(f"  - Redis event bus: {settings.redis_url}")

    if api_url:
        print(f"  - API URL: {api_url} (SSE events enabled)")

    # Create agent without ticker (ticker runs in worker/api, not bot)
    agent = NymeriaAgent(tools=get_all_tools_with_agents(), enable_ticker=False)
    agent.sync_agent_tools()

    # Create and run bot
    bot = NymeriaDiscordBot(
        agent=agent,
        respond_mode=settings.discord_respond_mode,
        api_url=api_url,
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


def run_mcp(args: argparse.Namespace) -> None:
    """Run the MCP server."""
    from nymeria.mcp_server import run_stdio, run_http

    if args.http:
        host = args.host or "127.0.0.1"
        port = args.port or 8001
        print(f"Starting Nymeria MCP server (HTTP mode) on {host}:{port}...")
        run_http(host=host, port=port)
    else:
        # STDIO mode - minimal output to avoid corrupting JSON-RPC
        run_stdio()


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
    python run.py service install    # Install as Windows service
    python run.py service start      # Start the Windows service
    python run.py service stop       # Stop the Windows service
    python run.py service status     # Check service status
    python run.py service run        # Run gateway in foreground (debug)
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

    # Service subcommand
    service_parser = subparsers.add_parser(
        "service",
        help="Windows service management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Actions:
    install    Install Nymeria as a Windows service
    uninstall  Remove the Windows service
    start      Start the Windows service
    stop       Stop the Windows service
    status     Check service status
    run        Run gateway in foreground (for debugging)
        """,
    )
    service_parser.add_argument(
        "action",
        choices=["install", "uninstall", "start", "stop", "status", "run"],
        help="Service action to perform",
    )

    args = parser.parse_args()

    # Setup logging (except for service commands which handle their own logging)
    if args.command != "service":
        setup_logging(args.log_level)

    # Validate configuration before running commands that need it
    # Skip validation for service status checks and help
    if args.command in ("cli", "api", "mcp", "worker", "discord-bot"):
        validate_config()
    elif args.command == "service" and args.action in ("install", "run"):
        validate_config()
    elif args.command == "service" and args.action == "status":
        # Status check doesn't need full validation
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
    elif args.command == "mcp":
        run_mcp(args)
    elif args.command == "service":
        run_service(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
