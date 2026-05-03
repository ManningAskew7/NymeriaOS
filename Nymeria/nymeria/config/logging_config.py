"""
Centralized logging configuration for Nymeria.

Controls log verbosity per subsystem via named profiles and per-module overrides.
All log level decisions flow through this module — run.py is the single caller.

Usage (env vars):
    LOG_LEVEL=INFO              # Base level for all loggers (default: INFO)
    LOG_PROFILES=llm,ticker     # Named debug profiles (comma-separated)
    LOG_MODULES=nymeria.core.agent:DEBUG,nymeria.tools.bash:WARNING

Available profiles:
    llm          - LLM call internals (messages sent, provider decisions)
    tools        - Tool call/result tracing
    agent        - Core agent orchestration (stream lifecycle, context loading)
    checkpoints  - SQLite/Postgres checkpoint read/write ops
    ticker       - Scheduled TODO polling and execution
    triggers     - Event-driven trigger checking and firing
    threads      - Callable thread orchestration (executor, tool_factory, agent)
    api          - HTTP request handling and schedule parsing
    sse          - SSE streaming events (autonomous + chat)
    compactor    - Auto-compaction and token tracking
    all          - Everything in nymeria.* at DEBUG
"""

import logging
import os
import re
import sys
from typing import Dict, List, Optional


_TOKEN_PATTERN = re.compile(r"nym_[A-Za-z0-9_-]{16,}")
_TOKEN_REDACTED = "nym_<redacted>"


class _TokenRedactingFilter(logging.Filter):
    """Redact raw Nymeria account tokens (``nym_...``) anywhere in a log record."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        try:
            if isinstance(record.msg, str):
                record.msg = _TOKEN_PATTERN.sub(_TOKEN_REDACTED, record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        k: _TOKEN_PATTERN.sub(_TOKEN_REDACTED, v) if isinstance(v, str) else v
                        for k, v in record.args.items()
                    }
                elif isinstance(record.args, tuple):
                    record.args = tuple(
                        _TOKEN_PATTERN.sub(_TOKEN_REDACTED, a) if isinstance(a, str) else a
                        for a in record.args
                    )
        except Exception:  # noqa: BLE001
            # Never let redaction kill a log line.
            pass
        return True


# ---------------------------------------------------------------------------
# Profile definitions
# ---------------------------------------------------------------------------
# Maps profile name -> list of (logger_name, level) tuples.
# Each module uses logger = logging.getLogger(__name__), so logger names
# exactly match dotted module paths.

LOG_PROFILES: Dict[str, List[tuple]] = {
    "llm": [
        ("nymeria.vendor.react_agent.nodes", logging.DEBUG),
        ("nymeria.vendor.react_agent.providers", logging.DEBUG),
    ],
    "tools": [
        ("nymeria.tools", logging.DEBUG),
        ("nymeria.core.thread_agent_executor", logging.DEBUG),
    ],
    "agent": [
        ("nymeria.core.agent", logging.DEBUG),
    ],
    "checkpoints": [
        ("nymeria.vendor.react_agent.graph", logging.DEBUG),
    ],
    "ticker": [
        ("nymeria.core.ticker", logging.DEBUG),
        ("nymeria.core.watchdog", logging.DEBUG),
    ],
    "triggers": [
        ("nymeria.core.trigger_manager", logging.DEBUG),
        ("nymeria.triggers.sources", logging.DEBUG),
        ("nymeria.triggers.trigger_api", logging.DEBUG),
    ],
    "api": [
        ("nymeria.triggers.api", logging.DEBUG),
    ],
    "sse": [
        ("nymeria.core.event_bus", logging.DEBUG),
        ("nymeria.core.event_bus_redis", logging.DEBUG),
        ("nymeria.triggers.api", logging.DEBUG),
    ],
    "compactor": [
        ("nymeria.core.agent_compaction", logging.DEBUG),
        ("nymeria.core.token_tracker", logging.DEBUG),
    ],
    "threads": [
        ("nymeria.core.thread_agent_executor", logging.DEBUG),
        ("nymeria.agents.tool_factory", logging.DEBUG),
        ("nymeria.core.agent", logging.DEBUG),
    ],
    "all": [
        ("nymeria", logging.DEBUG),
    ],
}

# ---------------------------------------------------------------------------
# Third-party loggers to suppress (always applied)
# ---------------------------------------------------------------------------
SUPPRESSED_THIRD_PARTY: Dict[str, int] = {
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "uvicorn.access": logging.WARNING,
    "uvicorn.error": logging.INFO,
    "langchain_core": logging.WARNING,
    "langgraph": logging.WARNING,
    "openai": logging.WARNING,
    "anthropic": logging.WARNING,
}


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------

class NymeriaFormatter(logging.Formatter):
    """Compact log formatter with short module names and optional ANSI color."""

    _COLORS = {
        logging.DEBUG:    "\033[36m",   # Cyan
        logging.INFO:     "\033[0m",    # Default
        logging.WARNING:  "\033[33m",   # Yellow
        logging.ERROR:    "\033[31m",   # Red
        logging.CRITICAL: "\033[35m",   # Magenta
    }
    _RESET = "\033[0m"

    _LEVEL_SHORT = {
        "DEBUG": "DEBG",
        "INFO": "INFO",
        "WARNING": "WARN",
        "ERROR": "ERRR",
        "CRITICAL": "CRIT",
    }

    def __init__(self, use_color: bool = False, short_names: bool = True):
        super().__init__()
        self.use_color = use_color
        self.short_names = short_names

    def _shorten_name(self, name: str) -> str:
        if not self.short_names:
            return name
        if name.startswith("nymeria."):
            name = name[len("nymeria."):]
        if name.startswith("vendor."):
            name = name[len("vendor."):]
        return name

    def format(self, record: logging.LogRecord) -> str:
        short_name = self._shorten_name(record.name)
        level = self._LEVEL_SHORT.get(record.levelname, record.levelname[:4])
        time_str = self.formatTime(record, "%H:%M:%S")
        msg = record.getMessage()

        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            msg = msg + "\n" + record.exc_text

        line = f"{time_str} {level:<4} {short_name}: {msg}"

        if self.use_color:
            color = self._COLORS.get(record.levelno, "")
            return f"{color}{line}{self._RESET}"

        return line


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def configure_logging(
    base_level: str = "INFO",
    profiles: Optional[List[str]] = None,
    module_overrides: Optional[Dict[str, int]] = None,
    file_handler: Optional[logging.Handler] = None,
    use_color: bool = True,
    short_names: bool = True,
) -> None:
    """
    Configure logging for the entire application. Called once by run.py.

    Args:
        base_level: Root nymeria logger level (default INFO — clean output).
        profiles:   Named profile names to enable at DEBUG.
        module_overrides: Dict of logger_name -> level for fine-grained control.
        file_handler: Optional file handler for service mode (added alongside console).
        use_color:  Apply ANSI colors to console output (auto-disabled for non-TTY).
        short_names: Strip 'nymeria.' prefix from logger names in output.
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.WARNING)
    # Close existing handlers before removing to avoid file descriptor leaks
    for handler in root_logger.handlers[:]:
        handler.close()
        root_logger.removeHandler(handler)

    # Shared filter — belt and suspenders. Tokens should never be logged, but
    # if one slips into a log line we redact it before the formatter runs.
    token_filter = _TokenRedactingFilter()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    formatter = NymeriaFormatter(use_color=use_color and is_tty, short_names=short_names)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.DEBUG)
    console_handler.addFilter(token_filter)
    root_logger.addHandler(console_handler)

    # Optional file handler (service mode)
    if file_handler is not None:
        file_formatter = NymeriaFormatter(use_color=False, short_names=short_names)
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(logging.DEBUG)
        file_handler.addFilter(token_filter)
        root_logger.addHandler(file_handler)

    # Nymeria base logger
    nymeria_level = getattr(logging, base_level.upper(), logging.INFO)
    logging.getLogger("nymeria").setLevel(nymeria_level)

    # Suppress third-party noise
    for logger_name, level in SUPPRESSED_THIRD_PARTY.items():
        logging.getLogger(logger_name).setLevel(level)

    # Apply named profiles
    active_profiles = profiles or []
    for profile_name in active_profiles:
        if profile_name not in LOG_PROFILES:
            logging.getLogger("nymeria.config.logging_config").warning(
                f"Unknown log profile '{profile_name}'. "
                f"Valid: {', '.join(sorted(LOG_PROFILES.keys()))}"
            )
            continue
        for logger_name, level in LOG_PROFILES[profile_name]:
            logging.getLogger(logger_name).setLevel(level)

    # Apply per-module overrides (highest priority)
    if module_overrides:
        for logger_name, level in module_overrides.items():
            logging.getLogger(logger_name).setLevel(level)


def parse_profiles_from_env() -> List[str]:
    """Parse LOG_PROFILES env var. Format: LOG_PROFILES=llm,ticker,checkpoints"""
    raw = os.environ.get("LOG_PROFILES", "").strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def parse_module_overrides_from_env() -> Dict[str, int]:
    """Parse LOG_MODULES env var. Format: LOG_MODULES=nymeria.core.agent:DEBUG,nymeria.tools.bash:WARNING"""
    raw = os.environ.get("LOG_MODULES", "").strip()
    if not raw:
        return {}

    overrides = {}
    for part in raw.split(","):
        part = part.strip()
        if ":" not in part:
            continue
        logger_name, level_str = part.rsplit(":", 1)
        level = getattr(logging, level_str.strip().upper(), None)
        if level is not None:
            overrides[logger_name.strip()] = level
    return overrides
