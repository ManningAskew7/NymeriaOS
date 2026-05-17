"""Telegram bot trigger for two-way Nymeria communication.

Thin client architecture: the bot calls the Nymeria REST API for all
operations (chat, tools, memory, etc.) instead of running its own
NymeriaAgent. This mirrors the Discord bot pattern — one agent, one
source of truth.

Supports bot commands, DM responses, reply-to-bot in groups, and SSE
streaming for progressive message editing and autonomous task delivery.
"""

import asyncio
import html as _html
import io
import json as _json
import logging
import re
import secrets
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence

import httpx
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, RetryAfter, TimedOut
from telegram.ext import (
    AIORateLimiter,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from nymeria.core.thread_classification import NATIVE_PLATFORM_PREFIXES as _NATIVE_SWITCH_THREAD_PREFIXES

from . import attachment_helpers
from .api_client import NymeriaAPIClient
from .bot_helpers import UserResolver, http_error_detail
from .message_splitter import split_telegram_message as split_message
from .sse_consumer import consume_sse_stream, parse_attach_paths as _parse_attach_paths
from ..core.service_health import HEARTBEAT_INTERVAL_SECONDS, write_service_heartbeat

logger = logging.getLogger(__name__)


# =============================================================================
# Telegram Formatting Helpers
# =============================================================================


def escape_html(text: str) -> str:
    """Escape text for Telegram HTML parse mode."""
    return _html.escape(str(text), quote=False)


def _mapping_sequence(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _tool_result_name(tool: Mapping[str, Any]) -> str:
    return str(tool.get("name") or tool.get("id") or tool.get("tool_id") or "")


def format_tool_search_html(data: Mapping[str, Any], *, limit: int = 8) -> str:
    """Format ranked tool search rows for Telegram replies and tests."""
    query = escape_html(str(data.get("query") or ""))
    mode = escape_html(str(data.get("mode") or "substring"))
    results = _mapping_sequence(data.get("results", []))
    lines = [f"<b>Tool Search</b> ({mode})"]
    if query:
        lines[0] += f": {query}"
    warning = str(data.get("warning") or "")
    if warning:
        lines.append(f"<i>{escape_html(warning[:240])}</i>")
    if not results:
        lines.append("No matching tools found.")
        return "\n".join(lines)
    for result in results[:limit]:
        name = _tool_result_name(result)
        category = str(result.get("category") or result.get("tool_type") or "tool")
        status = str(result.get("status") or "available")
        desc = str(result.get("description") or "").split("\n")[0][:100]
        hint = str(result.get("enable_hint") or f"/tools_enable {name}")
        hint = hint.replace("/tools enable ", "/tools_enable ")
        hint = hint.replace("/tools disable ", "/tools_disable ")
        lines.append(
            f"\n<code>{escape_html(name)}</code> "
            f"({escape_html(category)}, {escape_html(status)})"
        )
        if desc:
            lines.append(escape_html(desc))
        lines.append(f"<code>{escape_html(hint)}</code>")
    return "\n".join(lines)


def markdown_to_html(text: str) -> str:
    """Convert common markdown patterns to Telegram HTML.

    Handles code blocks, inline code, bold, italic, strikethrough,
    and blockquotes. Falls back gracefully — if conversion produces
    invalid HTML, callers should retry without parse_mode.
    """
    if not text:
        return text

    # Step 1: Extract fenced code blocks and inline code to protect them
    code_blocks: List[str] = []
    inline_codes: List[str] = []

    def _replace_fenced(m):
        lang = m.group(1) or ""
        code = m.group(2)
        idx = len(code_blocks)
        escaped = escape_html(code)
        if lang:
            code_blocks.append(f"<pre><code class=\"language-{escape_html(lang)}\">{escaped}</code></pre>")
        else:
            code_blocks.append(f"<pre>{escaped}</pre>")
        return f"\x00CODEBLOCK{idx}\x00"

    def _replace_inline(m):
        code = m.group(1)
        idx = len(inline_codes)
        inline_codes.append(f"<code>{escape_html(code)}</code>")
        return f"\x00INLINE{idx}\x00"

    # Fenced code blocks: ```lang\ncode\n```
    result = re.sub(r"```(\w*)\n(.*?)```", _replace_fenced, text, flags=re.DOTALL)
    # Inline code: `code`
    result = re.sub(r"`([^`\n]+)`", _replace_inline, result)

    # Step 2: Escape HTML entities in remaining text
    result = escape_html(result)

    # Step 3: Convert markdown patterns
    # Bold: **text** or __text__
    result = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", result)
    result = re.sub(r"__(.+?)__", r"<b>\1</b>", result)
    # Italic: *text* or _text_ (but not inside words with underscores)
    result = re.sub(r"(?<!\w)\*([^*\n]+?)\*(?!\w)", r"<i>\1</i>", result)
    result = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"<i>\1</i>", result)
    # Strikethrough: ~~text~~
    result = re.sub(r"~~(.+?)~~", r"<s>\1</s>", result)

    # Blockquotes: lines starting with >
    lines = result.split("\n")
    i = 0
    new_lines = []
    while i < len(lines):
        if lines[i].startswith("&gt; "):
            # Collect consecutive blockquote lines
            bq_lines = []
            while i < len(lines) and lines[i].startswith("&gt; "):
                bq_lines.append(lines[i][5:])  # Strip "&gt; "
                i += 1
            new_lines.append(f"<blockquote>{chr(10).join(bq_lines)}</blockquote>")
        else:
            new_lines.append(lines[i])
            i += 1
    result = "\n".join(new_lines)

    # Step 4: Restore code blocks and inline code
    for idx, block in enumerate(code_blocks):
        result = result.replace(f"\x00CODEBLOCK{idx}\x00", block)
    for idx, code in enumerate(inline_codes):
        result = result.replace(f"\x00INLINE{idx}\x00", code)

    return result


def format_tool_call_html(name: str, args: Any, max_args_len: int = 800) -> str:
    """Render a tool-call announcement as Telegram HTML."""
    args_str = ""
    if args:
        try:
            args_str = _json.dumps(args, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            args_str = str(args)
    if len(args_str) > max_args_len:
        args_str = args_str[: max_args_len - 3] + "..."
    text = f"<b>Tool: {escape_html(name)}</b>"
    if args_str:
        text += f"\n<pre>{escape_html(args_str)}</pre>"
    return text


def format_tool_result_html(result: Any, max_len: int = 800) -> str:
    """Render a tool-result block as Telegram HTML."""
    result_str = str(result) if result is not None else ""
    if not result_str:
        return "<i>(empty result)</i>"
    if len(result_str) > max_len:
        result_str = result_str[: max_len - 3] + "..."
    return f"<b>Result:</b>\n<pre>{escape_html(result_str)}</pre>"


def format_compaction_notice_html(
    summary: Any,
    messages_removed: int = 0,
    max_summary_len: int = 900,
    title: str = "Context compacted",
) -> str:
    """Render a compact Telegram notice for compaction events."""
    parts = [f"<b>{escape_html(title)}</b>"]
    if messages_removed:
        parts.append(f"<i>{messages_removed} messages summarized.</i>")
    summary_text = str(summary or "").strip()
    if summary_text:
        if len(summary_text) > max_summary_len:
            summary_text = summary_text[: max_summary_len - 3].rstrip() + "..."
        parts.append(f"<blockquote>{escape_html(summary_text)}</blockquote>")
    return "\n".join(parts)


parse_attach_paths = _parse_attach_paths


# =============================================================================
# Thread / User ID Helpers
# =============================================================================


def make_thread_id(chat_id: int) -> str:
    """Generate a Nymeria thread ID from a Telegram chat ID."""
    return f"telegram_{chat_id}"


# How often to re-fetch the full per-thread chat<->thread binding map from
# the API. Direct cache mutation on /bind and /unbind makes those changes
# feel instant; this loop catches bindings created from the desktop wizard.
_BINDING_REFRESH_INTERVAL_SECONDS = 60
_THREAD_CONFIG_CACHE_TTL_SECONDS = 60

# How often the shared bot reconciles its set of running user-owned bots
# against the API. Determines how long a freshly-registered BYO bot takes
# to come online (the wizard polls /me/telegram-bots/{id} for last_seen_at
# during this window). 15s is the floor that keeps the wizard's UX snappy
# without hammering the API.
_USER_BOT_SUPERVISOR_INTERVAL_SECONDS = 15

# Telegram accepts up to 4096 characters in a text message. Keep generated
# chunks below that to leave room for HTML tags/entities added by formatting.
TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_SAFE_CHUNK_LENGTH = 3500
THREAD_PICKER_CACHE_TTL_SECONDS = 10 * 60
THREAD_PICKER_LIMIT = 15
STOP_BUTTON_TOKEN_TTL_SECONDS = 60 * 60

COMMAND_ACCESS_PUBLIC = "public"
COMMAND_ACCESS_LINKED = "linked"
COMMAND_ACCESS_ADMIN = "admin"

TELEGRAM_COMMAND_ACCESS: Mapping[str, str] = {
    # Public onboarding/help commands.
    "start": COMMAND_ACCESS_PUBLIC,
    "help": COMMAND_ACCESS_PUBLIC,
    "bind": COMMAND_ACCESS_PUBLIC,
    # Admin-only global controls.
    "think": COMMAND_ACCESS_ADMIN,
    "restart": COMMAND_ACCESS_ADMIN,
    "config_show": COMMAND_ACCESS_ADMIN,
    "config_get": COMMAND_ACCESS_ADMIN,
    "config_set": COMMAND_ACCESS_ADMIN,
    "env_show": COMMAND_ACCESS_ADMIN,
    "env_set": COMMAND_ACCESS_ADMIN,
    # Everything else requires a linked Telegram identity.
}

TELEGRAM_LOCAL_COMMANDS: tuple[tuple[str, str, str], ...] = (
    ("Chat", "ask", "Send a message to Nymeria"),
    ("Chat", "stop", "Abort current operation"),
    ("Chat", "clear", "Clear conversation history"),
    ("Chat", "compact", "Compress conversation context"),
    ("Chat", "export", "Export conversation history"),
    ("Chat", "restart", "Restart bot or API"),
    ("Chat", "showtools", "Toggle tool call display"),
    ("Chat", "help", "Show all commands"),
    ("Thread Binding", "bind", "Bind this chat to a desktop thread"),
    ("Thread Binding", "threads", "List switchable threads"),
    ("Thread Binding", "switch", "Switch this chat to a thread"),
    ("Thread Binding", "new", "Start a fresh thread"),
    ("Thread Binding", "unbind", "Remove this chat's thread binding"),
    ("Tools", "tools_search", "Search tools"),
)


def _service_command_catalog(*, is_admin: bool = True) -> list[dict[str, Any]]:
    from ..core.command_service import get_command_service

    return [
        asdict(info)
        for info in get_command_service().list_commands(
            actor="user",
            surface="telegram",
            is_admin=is_admin,
        )
    ]


def _telegram_command_name(info: Mapping[str, Any]) -> str:
    path_value = info.get("path")
    if isinstance(path_value, Sequence) and not isinstance(path_value, (str, bytes)):
        path = [str(part).strip().lower() for part in path_value if str(part).strip()]
    else:
        path = [part for part in str(info.get("name") or "").lower().split() if part]
    if not path:
        return ""
    if len(path) == 1:
        return path[0]
    if path[0] == "todos":
        return f"todo_{path[1]}"

    aliases = info.get("aliases") or []
    if isinstance(aliases, Sequence) and not isinstance(aliases, (str, bytes)):
        for alias in aliases:
            normalized = str(alias).strip().lstrip("/")
            if normalized and " " not in normalized:
                return normalized
    return "_".join(path)


def _telegram_global_catalog_entries(
    commands: Sequence[Mapping[str, Any]],
) -> list[tuple[str, str, str]]:
    entries: list[tuple[str, str, str]] = []
    for info in commands:
        if str(info.get("execution_kind") or "command") != "command":
            continue
        name = _telegram_command_name(info)
        if not name or name in {"help"}:
            continue
        description = str(info.get("description") or "")
        category = str(info.get("category") or "Global")
        entries.append((category, name, description))
    return entries


def _merged_telegram_catalog(
    commands: Sequence[Mapping[str, Any]],
) -> list[tuple[str, str, str]]:
    entries: dict[str, tuple[str, str, str]] = {}
    for category, name, description in _telegram_global_catalog_entries(commands):
        entries[name] = (category, name, description)
    for category, name, description in TELEGRAM_LOCAL_COMMANDS:
        entries[name] = (category, name, description)
    return sorted(entries.values(), key=lambda item: (item[0].casefold(), item[1]))


def _telegram_bot_commands_from_catalog(
    commands: Sequence[Mapping[str, Any]],
) -> list[BotCommand]:
    return [
        BotCommand(name, description[:256] or "Nymeria command")
        for _category, name, description in _merged_telegram_catalog(commands)
    ]


@dataclass(frozen=True, slots=True)
class _StopButtonToken:
    chat_id: int
    thread_id: str
    nymeria_user_id: str
    telegram_user_id: Optional[int]
    expires_at: float


def _thread_title(thread: dict) -> str:
    title = str(thread.get("title") or "").strip()
    return title or "New Chat"


def _normalize_thread_label(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _is_switchable_thread(thread: dict) -> bool:
    thread_id = str(thread.get("thread_id") or "")
    return bool(thread_id) and not thread_id.startswith(_NATIVE_SWITCH_THREAD_PREFIXES)


def _thread_sort_key(thread: dict) -> tuple:
    return (
        bool(thread.get("pinned")),
        str(thread.get("updated_at") or thread.get("created_at") or ""),
        _normalize_thread_label(_thread_title(thread)),
        str(thread.get("thread_id") or ""),
    )


def _sorted_switchable_threads(threads: List[dict]) -> List[dict]:
    return sorted(
        [thread for thread in threads if _is_switchable_thread(thread)],
        key=_thread_sort_key,
        reverse=True,
    )


def _find_thread_match(
    threads: List[dict],
    query: str,
    cached_threads: Optional[List[dict]] = None,
) -> tuple[Optional[dict], List[dict], str]:
    """Find a switch target by number, title, or thread id.

    Returns ``(match, ambiguous_matches, reason)``.
    """
    needle = query.strip()
    if not needle:
        return None, [], "empty"

    if needle.isdigit():
        index = int(needle) - 1
        choices = cached_threads or _sorted_switchable_threads(threads)
        if 0 <= index < len(choices):
            return choices[index], [], "number"
        if cached_threads is not None:
            return None, [], "number_out_of_range"

    candidates = _sorted_switchable_threads(threads)
    exact_id = [t for t in candidates if str(t.get("thread_id") or "") == needle]
    if exact_id:
        return exact_id[0], [], "exact_id"

    normalized = _normalize_thread_label(needle)
    exact_title = [
        t for t in candidates
        if _normalize_thread_label(_thread_title(t)) == normalized
    ]
    if len(exact_title) == 1:
        return exact_title[0], [], "exact_title"
    if len(exact_title) > 1:
        return None, exact_title, "ambiguous"

    id_prefix = [
        t for t in candidates
        if str(t.get("thread_id") or "").startswith(needle)
    ]
    if len(id_prefix) == 1:
        return id_prefix[0], [], "id_prefix"
    if len(id_prefix) > 1:
        return None, id_prefix, "ambiguous"

    title_contains = [
        t for t in candidates
        if normalized in _normalize_thread_label(_thread_title(t))
    ]
    if len(title_contains) == 1:
        return title_contains[0], [], "title_contains"
    if len(title_contains) > 1:
        return None, title_contains, "ambiguous"

    return None, [], "not_found"


# =============================================================================
# Telegram Bot Client
# =============================================================================


class NymeriaTelegramBot:
    """Telegram bot client -- thin API client for Nymeria."""

    def __init__(
        self,
        api: NymeriaAPIClient,
        bot_token: str,
        default_chat_id: Optional[str] = None,
        *,
        user_telegram_bot_id: Optional[int] = None,
        bot_owner_user_id: Optional[str] = None,
        owns_api_client: bool = True,
    ):
        self.api = api
        self.bot_token = bot_token
        self.default_chat_id = default_chat_id
        self._owns_api_client = owns_api_client
        # Multi-bot identity. ``user_telegram_bot_id is None`` is the shared
        # bot; non-None means this instance serves a user-owned BYO bot
        # whose token came from the wizard. The shared bot also acts as the
        # supervisor for user-owned bots — it owns the SSE listener and the
        # ``_user_bots`` registry; user-owned bots skip those.
        self.user_telegram_bot_id = user_telegram_bot_id
        self.bot_owner_user_id = bot_owner_user_id
        self._start_time = time.time()
        self._show_tool_calls: Dict[int, bool] = {}  # chat_id -> show
        # Per-thread streaming state for autonomous task delivery.
        # thread_id -> { chat_id, buffer (response text), tool_count, response_seen }
        self._autonomous_state: Dict[str, Dict[str, Any]] = {}
        self._application = None
        self._user_resolver = UserResolver(self.api, "telegram", logger=logger)
        # thread_id -> (telegram_autonomous_delivery, expires_at)
        self._thread_delivery_cache: Dict[str, tuple[str, float]] = {}
        # Per-thread chat-app bindings — populated by _refresh_bindings on
        # startup and refreshed every _BINDING_REFRESH_INTERVAL_SECONDS to
        # absorb bindings created from the desktop wizard. Direct mutation
        # in /bind and /unbind handlers makes those changes feel instant.
        # Filtered to bindings served by THIS bot (user_telegram_bot_id matches).
        self._bindings: Dict[int, str] = {}              # chat_id -> thread_id
        self._reverse_bindings: Dict[str, int] = {}      # thread_id -> chat_id
        # chat_id -> (expires_at_monotonic, thread choices). Lets users run
        # /threads first, then /switch 2 without relying on fragile titles.
        self._thread_picker_cache: Dict[int, tuple[float, List[dict]]] = {}
        # Opaque stop-button callback tokens. Telegram callback data is visible
        # to clients, so store thread/user authorization server-side.
        self._stop_button_tokens: Dict[str, _StopButtonToken] = {}
        # Shared-bot only: subordinate user-owned bots, keyed by row id.
        # Always empty on user-owned bot instances.
        self._user_bots: Dict[int, "NymeriaTelegramBot"] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._health_task: Optional[asyncio.Task] = None

    async def _get_telegram_autonomous_delivery(self, thread_id: str) -> str:
        """Read and cache this thread's Telegram autonomous delivery mode."""
        now = time.time()
        cached = self._thread_delivery_cache.get(thread_id)
        if cached and cached[1] > now:
            return cached[0]
        mode = "full"
        try:
            cfg = await self.api.get_thread_config(thread_id)
            candidate = (cfg or {}).get("telegram_autonomous_delivery")
            if candidate in ("full", "notify_only", "off"):
                mode = candidate
        except Exception as e:
            logger.debug("Failed to load Telegram delivery mode for %s: %s", thread_id, e)
        self._thread_delivery_cache[thread_id] = (
            mode,
            now + _THREAD_CONFIG_CACHE_TTL_SECONDS,
        )
        return mode

    @property
    def is_shared_bot(self) -> bool:
        return self.user_telegram_bot_id is None

    async def resolve_user_id(self, telegram_user_id: int) -> Optional[str]:
        """Resolve a Telegram user id to a linked Nymeria account, or None.

        Caches the result (including ``None`` for confirmed-unlinked users)
        so admin relinks propagate without a restart.
        """
        return await self._user_resolver.resolve(telegram_user_id)

    # =========================================================================
    # Per-thread chat-app bindings (Telegram chat <-> Nymeria thread)
    # =========================================================================

    def resolve_thread_id_for_chat(self, chat_id: int) -> str:
        """Return the thread id for inbound messages from this chat.

        If the chat has been bound via the desktop wizard or the ``/bind``
        command, returns the bound thread. Otherwise falls back to the
        legacy ``telegram_<chat_id>`` default — so chats with no explicit
        binding keep working exactly as before.
        """
        bound = self._bindings.get(int(chat_id))
        return bound if bound is not None else make_thread_id(chat_id)

    def resolve_chat_id_for_thread(self, thread_id: str) -> Optional[int]:
        """Reverse lookup: bound chat_id for an outbound thread, or None.

        Used by the autonomous-event SSE listener to dispatch events whose
        ``thread_id`` is a non-default UUID (i.e. a desktop-created thread
        bound to a Telegram chat).
        """
        return self._reverse_bindings.get(thread_id)

    async def _refresh_bindings(self) -> None:
        """Pull the current chat<->thread map from the API into local cache.

        Each bot only caches bindings it serves: the shared bot keeps rows
        with ``user_telegram_bot_id IS NULL``; a user-owned bot keeps rows
        with its own id. This keeps inbound resolution clean — a chat from
        a different bot's polling never resolves to anything in this cache.

        Builds new dicts locally and atomically swaps them in to avoid
        partial-state reads from concurrent lookups.
        """
        try:
            entries = await self.api.list_chatapp_bindings(provider="telegram")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to refresh chat-app bindings; keeping current cache")
            return
        new_chat_to_thread: Dict[int, str] = {}
        new_thread_to_chat: Dict[str, int] = {}
        for e in entries:
            # Filter to bindings this bot serves. The API returns
            # user_telegram_bot_id as None or an integer.
            entry_bot_id = e.get("user_telegram_bot_id")
            if entry_bot_id != self.user_telegram_bot_id:
                continue
            try:
                chat_id = int(e.get("platform_chat_id"))
            except (TypeError, ValueError):
                continue
            thread_id = e.get("thread_id")
            if not thread_id:
                continue
            new_chat_to_thread[chat_id] = thread_id
            new_thread_to_chat[thread_id] = chat_id
        # Atomic swap (no awaits between the two assignments).
        self._bindings = new_chat_to_thread
        self._reverse_bindings = new_thread_to_chat
        logger.debug(
            "chat-app bindings refreshed for bot=%s: %d entries",
            self.user_telegram_bot_id,
            len(new_chat_to_thread),
        )

    async def _bindings_refresh_loop(self) -> None:
        """Background task: refresh the binding cache every minute."""
        while True:
            try:
                await asyncio.sleep(_BINDING_REFRESH_INTERVAL_SECONDS)
                await self._refresh_bindings()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                logger.exception("bindings refresh loop tick failed")

    # =========================================================================
    # User-owned bot supervision (shared-bot only)
    # =========================================================================

    async def _user_bots_supervisor_loop(self) -> None:
        """Periodically reconcile our running set of user-owned bots with
        the API's view of registered bots. Runs only on the shared bot.
        """
        if not self.is_shared_bot:
            return
        while True:
            try:
                await self._refresh_user_bots()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                logger.exception("user-bot supervisor refresh failed")
            await asyncio.sleep(_USER_BOT_SUPERVISOR_INTERVAL_SECONDS)

    async def _refresh_user_bots(self) -> None:
        """Diff registered bots against the running set; start new bots,
        stop deleted ones, leave unchanged ones alone.
        """
        try:
            entries = await self.api.list_admin_telegram_bots()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to list user-owned bots from API")
            return
        wanted = {int(e["id"]): e for e in entries if e.get("enabled")}
        # Stop bots that have disappeared
        for bot_id in list(self._user_bots.keys()):
            if bot_id not in wanted:
                logger.info("Stopping user-owned bot id=%s (removed)", bot_id)
                try:
                    await self._user_bots[bot_id].stop_async()
                except Exception:  # noqa: BLE001
                    logger.exception("Error stopping user-owned bot id=%s", bot_id)
                self._user_bots.pop(bot_id, None)
        # Start bots that appeared
        for bot_id, entry in wanted.items():
            if bot_id in self._user_bots:
                continue
            sub = NymeriaTelegramBot(
                api=self.api,
                bot_token=entry["bot_token"],
                user_telegram_bot_id=bot_id,
                bot_owner_user_id=entry.get("owner_user_id"),
                owns_api_client=False,
            )
            try:
                await sub.start_async()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Failed to start user-owned bot id=%s @%s",
                    bot_id,
                    entry.get("bot_username"),
                )
                continue
            self._user_bots[bot_id] = sub
            logger.info(
                "Started user-owned bot id=%s @%s",
                bot_id,
                entry.get("bot_username"),
            )
            # Heartbeat so the wizard can detect "bot is alive"
            try:
                await self.api.report_telegram_bot_seen(bot_id)
            except Exception:  # noqa: BLE001
                logger.warning("Heartbeat after start failed for bot=%s", bot_id)

    # =========================================================================
    # Async lifecycle for non-blocking bot startup (used by sub-bots)
    # =========================================================================

    async def start_async(self) -> None:
        """Build the Application and start polling, non-blocking.

        Used for user-owned bots managed by a supervisor. The shared bot
        still uses the blocking ``run()`` path because it owns the asyncio
        event loop. This method assumes it's called from inside an already-
        running event loop.
        """
        if self._application is not None:
            return  # already started
        app = (
            ApplicationBuilder()
            .token(self.bot_token)
            .rate_limiter(AIORateLimiter())
            .post_shutdown(self._post_shutdown)
            .build()
        )
        self._application = app
        self._register_handlers(app)
        await app.initialize()
        await self._post_init(app)
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    async def stop_async(self) -> None:
        """Graceful teardown — opposite of ``start_async``."""
        app = self._application
        if app is None:
            return
        try:
            if app.updater is not None:
                await app.updater.stop()
            if app.running:
                await app.stop()
            await app.shutdown()
        finally:
            await self._cleanup_after_shutdown(close_api=self._owns_api_client)

    def _spawn_background_task(self, coro) -> asyncio.Task:
        """Start a background task and keep a handle for graceful shutdown."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def _cancel_background_tasks(self) -> None:
        current = asyncio.current_task()
        tasks = [
            task
            for task in self._background_tasks
            if task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.difference_update(tasks)

    async def _cleanup_after_shutdown(self, *, close_api: bool) -> None:
        await self._cancel_background_tasks()
        for bot_id, bot in list(self._user_bots.items()):
            try:
                await bot.stop_async()
            except Exception:  # noqa: BLE001
                logger.exception("Error stopping user-owned bot id=%s", bot_id)
        self._user_bots.clear()

        if close_api:
            close = getattr(self.api, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    logger.warning("Failed to close Telegram API client", exc_info=True)
        self._application = None

    async def _post_shutdown(self, application) -> None:
        """Close Nymeria-side resources after python-telegram-bot stops."""
        await self._cleanup_after_shutdown(close_api=self._owns_api_client)

    async def _request_self_restart(self) -> None:
        """Gracefully stop polling so the process can exit and be restarted."""
        app = self._application
        if app is None:
            await self._cleanup_after_shutdown(close_api=self._owns_api_client)
            raise SystemExit(0)
        app.stop_running()

    async def _resolve_or_reject_update(
        self,
        update: "Update",
        *,
        require_admin: bool = False,
    ) -> Optional[str]:
        """Resolve the slash-command caller's Nymeria user_id.

        Returns ``user_id`` on success. On failure (unlinked Telegram user, or
        ``require_admin=True`` and caller isn't an admin) replies with a
        rejection and returns ``None``. The caller should ``return``
        immediately when this returns ``None``.
        """
        tg_user = update.effective_user
        if tg_user is None:
            return None
        user_id = await self.resolve_user_id(tg_user.id)

        async def _reply(msg: str) -> None:
            try:
                if update.message is not None:
                    await update.message.reply_text(msg)
                elif update.effective_chat is not None:
                    await update.effective_chat.send_message(msg)
            except Exception as e:  # noqa: BLE001
                logger.warning("Could not send rejection: %s", e)

        if user_id is None:
            await _reply(
                "This Telegram account isn't linked to a Nymeria user yet. "
                "Ask the admin to run: "
                f"`python run.py users link-platform <email> telegram {tg_user.id}`"
            )
            return None

        if require_admin:
            try:
                me = await self.api.get_me(act_as=user_id)
                if me.get("role") != "admin":
                    await _reply("Admin only.")
                    return None
            except Exception as e:  # noqa: BLE001
                logger.warning("Admin check failed for %s: %s", user_id, e)
                await _reply("Couldn't verify permissions; try again later.")
                return None

        return user_id

    def _command_access_for(self, name: str) -> str:
        """Return the access policy for a Telegram command."""
        return TELEGRAM_COMMAND_ACCESS.get(name, COMMAND_ACCESS_LINKED)

    async def _check_command_access(
        self,
        name: str,
        update: "Update",
    ) -> bool:
        """Apply the command access policy before a handler runs."""
        access = self._command_access_for(name)
        if access == COMMAND_ACCESS_PUBLIC:
            return True
        user_id = await self._resolve_or_reject_update(
            update,
            require_admin=access == COMMAND_ACCESS_ADMIN,
        )
        return user_id is not None

    def _guarded_command(
        self,
        name: str,
        handler: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]],
    ) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]:
        """Wrap a command handler with the central Telegram access policy."""

        async def guarded(
            update: Update,
            context: ContextTypes.DEFAULT_TYPE,
        ) -> None:
            if await self._check_command_access(name, update):
                await handler(update, context)

        return guarded

    def _prune_stop_button_tokens(self) -> None:
        now = time.monotonic()
        expired = [
            token
            for token, record in self._stop_button_tokens.items()
            if record.expires_at <= now
        ]
        for token in expired:
            self._stop_button_tokens.pop(token, None)

    def _create_stop_button_token(
        self,
        *,
        chat_id: int,
        thread_id: str,
        nymeria_user_id: str,
        telegram_user_id: Optional[int],
    ) -> str:
        """Create an opaque callback token for a streaming stop button."""
        self._prune_stop_button_tokens()
        token = secrets.token_urlsafe(16)
        self._stop_button_tokens[token] = _StopButtonToken(
            chat_id=int(chat_id),
            thread_id=thread_id,
            nymeria_user_id=nymeria_user_id,
            telegram_user_id=telegram_user_id,
            expires_at=time.monotonic() + STOP_BUTTON_TOKEN_TTL_SECONDS,
        )
        return token

    def run(self) -> None:
        """Build the Application, register handlers, and start polling."""
        app = (
            ApplicationBuilder()
            .token(self.bot_token)
            .rate_limiter(AIORateLimiter())
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
            .build()
        )
        self._application = app
        self._register_handlers(app)
        app.run_polling(allowed_updates=Update.ALL_TYPES)

    async def _post_init(self, application) -> None:
        """Register command menu with Telegram and start SSE listener."""
        commands = _telegram_bot_commands_from_catalog(_service_command_catalog())
        await application.bot.set_my_commands(commands)
        logger.info(
            "Registered %d bot commands with Telegram (bot=%s)",
            len(commands),
            self.user_telegram_bot_id if not self.is_shared_bot else "shared",
        )

        # Populate this bot's binding cache before any listener starts.
        await self._refresh_bindings()
        logger.info(
            "Loaded %d chat-app binding(s) for bot=%s",
            len(self._bindings),
            self.user_telegram_bot_id if not self.is_shared_bot else "shared",
        )

        # Start the per-bot binding refresh.
        self._spawn_background_task(self._bindings_refresh_loop())

        # The SSE listener and the user-bot supervisor live ONLY on the
        # shared bot. User-owned bots receive autonomous events via the
        # shared bot dispatching to their `_handle_sse_event`.
        if self.is_shared_bot:
            self._start_health_heartbeat()
            self._spawn_background_task(self._api_sse_listener())
            self._spawn_background_task(self._user_bots_supervisor_loop())

    def _start_health_heartbeat(self) -> None:
        if self._health_task is not None and not self._health_task.done():
            return
        self._health_task = self._spawn_background_task(self._health_heartbeat_loop())

    async def _health_heartbeat_loop(self) -> None:
        """Publish health only while polling and the Nymeria API are usable."""
        while True:
            try:
                app = self._application
                updater = getattr(app, "updater", None) if app is not None else None
                app_running = bool(getattr(app, "running", False))
                polling_running = bool(getattr(updater, "running", False))
                api_ok = await self.api.health()
                write_service_heartbeat(
                    "telegram-bot",
                    status="ok" if api_ok and app_running and polling_running else "unhealthy",
                    details={
                        "api_ok": api_ok,
                        "app_running": app_running,
                        "polling_running": polling_running,
                        "user_bot_count": len(self._user_bots),
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Telegram health heartbeat failed", exc_info=True)
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

    def _register_handlers(self, app) -> None:
        """Register all command and message handlers."""
        def command(name: str, handler) -> None:
            app.add_handler(CommandHandler(name, self._guarded_command(name, handler)))

        # Chat commands
        command("ask", self._cmd_ask)
        command("stop", self._cmd_stop)
        command("clear", self._cmd_clear)
        command("compact", self._cmd_compact)
        command("thread", self._cmd_thread)
        command("status", self._cmd_status)
        command("model", self._cmd_model)
        command("models", self._cmd_models)
        command("think", self._cmd_think)
        command("context", self._cmd_context)
        command("tasks", self._cmd_tasks)
        command("export", self._cmd_export)
        command("restart", self._cmd_restart)
        command("showtools", self._cmd_showtools)
        command("help", self._cmd_help)
        command("start", self._cmd_start)

        # TODO commands
        command("todo_add", self._cmd_todo_add)
        command("todo_list", self._cmd_todo_list)
        command("todo_complete", self._cmd_todo_complete)
        command("todo_delete", self._cmd_todo_delete)

        # Config commands
        command("config_show", self._cmd_config_show)
        command("config_get", self._cmd_config_get)
        command("config_set", self._cmd_config_set)

        # Env commands
        command("env_show", self._cmd_env_show)
        command("env_set", self._cmd_env_set)

        # Tools commands
        command("tools_core", self._cmd_tools_core)
        command("tools_optional", self._cmd_tools_optional)
        command("tools_enabled", self._cmd_tools_enabled)
        command("tools_search", self._cmd_tools_search)
        command("tools_category", self._cmd_tools_category)
        command("tools_enable", self._cmd_tools_enable)
        command("tools_disable", self._cmd_tools_disable)

        # Memory commands
        command("memory_list", self._cmd_memory_list)
        command("memory_save", self._cmd_memory_save)
        command("memory_forget", self._cmd_memory_forget)
        command("memory_search", self._cmd_memory_search)

        # Notepad commands
        command("notepad_read", self._cmd_notepad_read)
        command("notepad_write", self._cmd_notepad_write)
        command("notepad_clear", self._cmd_notepad_clear)

        # Chat-app binding commands
        command("bind", self._cmd_bind)
        command("threads", self._cmd_threads)
        command("switch", self._cmd_switch)
        command("new", self._cmd_new)
        command("unbind", self._cmd_unbind)

        # Callback query handler (stop button)
        app.add_handler(CallbackQueryHandler(self._on_stop_button, pattern=r"^stop:"))

        # Plain text messages, photos, and document uploads
        # (DMs and replies-to-bot in groups). Captions on photos/documents
        # are surfaced via update.message.caption inside the handler.
        app.add_handler(MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND,
            self._on_message,
        ))

        # Error handler
        app.add_error_handler(self._error_handler)

    # =========================================================================
    # Helpers
    # =========================================================================

    async def _send_html(
        self,
        chat_id: int,
        text: str,
        context: Optional[ContextTypes.DEFAULT_TYPE] = None,
        **kwargs,
    ) -> Message:
        """Send a message with HTML parse mode, falling back to plain text.

        If ``context`` is omitted, the bot is taken from ``self._application``
        so background tasks (e.g. the autonomous SSE listener) can use this
        helper without a Telegram update context.
        """
        bot = context.bot if context is not None else self._application.bot
        try:
            return await bot.send_message(
                chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, **kwargs
            )
        except BadRequest:
            # HTML parsing failed — send as plain text
            # Strip HTML tags for readable fallback
            plain = re.sub(r"<[^>]+>", "", text)
            return await bot.send_message(
                chat_id=chat_id, text=plain or text, **kwargs
            )

    async def _edit_html(
        self, msg: Message, text: str, **kwargs
    ) -> None:
        """Edit a message with HTML parse mode, falling back to plain text."""
        try:
            await msg.edit_text(text=text, parse_mode=ParseMode.HTML, **kwargs)
        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                return
            if "can't parse" in str(e).lower():
                plain = re.sub(r"<[^>]+>", "", text)
                try:
                    await msg.edit_text(text=plain or text, **kwargs)
                except BadRequest:
                    pass  # HTML fallback to plain text already handled above
            # Other BadRequest (message too old, etc.) — ignore
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except TimedOut:
            pass  # timeout during retry-after wait is benign

    async def _send_file_attachment(
        self,
        chat_id: int,
        file_path: str,
        context: Optional[ContextTypes.DEFAULT_TYPE] = None,
    ) -> bool:
        """Download a workspace file and send it to the Telegram chat."""
        result = await self.api.download_workspace_file(file_path)
        if result is None:
            return False
        raw_bytes, filename, content_type = result
        bot = context.bot if context is not None else self._application.bot
        buf = io.BytesIO(raw_bytes)
        buf.name = filename
        try:
            if content_type.startswith("image/") and len(raw_bytes) < 10 * 1024 * 1024:
                await bot.send_photo(chat_id=chat_id, photo=buf, caption=filename)
            else:
                await bot.send_document(chat_id=chat_id, document=buf, caption=filename)
            return True
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
            return False
        except Exception as e:
            logger.warning("Failed to send file attachment %s: %s", file_path, e)
            return False

    def _parse_args(self, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Get the text after the command."""
        return " ".join(context.args) if context.args else ""

    async def _send_backend_command(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        command_path: str,
        *,
        require_admin: bool = False,
    ) -> None:
        """Execute a global backend command and relay its markdown result."""
        user_id = await self._resolve_or_reject_update(
            update,
            require_admin=require_admin,
        )
        if user_id is None:
            return
        chat_id = update.effective_chat.id
        thread_id = self.resolve_thread_id_for_chat(chat_id)
        args = self._parse_args(context)
        raw_command = f"/{command_path}"
        if args:
            raw_command += f" {args}"

        try:
            result = await self.api.execute_command(
                raw_command,
                thread_id=thread_id,
                source="user",
                actor="user",
                surface="telegram",
                user_id=user_id,
            )
        except Exception as e:
            logger.error(
                "Backend command failed for Telegram /%s: %s",
                command_path,
                e,
                exc_info=True,
            )
            await update.message.reply_text(f"Error: {e}")
            return

        text = str(result.get("markdown") or "").strip()
        if not text:
            text = "Done." if result.get("success") else "Command returned no output."
        for chunk in split_message(text, TELEGRAM_TEXT_LIMIT):
            await context.bot.send_message(chat_id=chat_id, text=chunk)

    def _set_local_binding(self, chat_id: int, thread_id: str) -> None:
        """Update this bot's in-memory chat<->thread maps after an API move."""
        chat_id = int(chat_id)
        for existing_thread_id, existing_chat_id in list(self._reverse_bindings.items()):
            if existing_chat_id == chat_id and existing_thread_id != thread_id:
                self._reverse_bindings.pop(existing_thread_id, None)
        self._bindings[chat_id] = thread_id
        self._reverse_bindings[thread_id] = chat_id

    def _get_cached_thread_choices(self, chat_id: int) -> Optional[List[dict]]:
        cached = self._thread_picker_cache.get(int(chat_id))
        if cached is None:
            return None
        expires_at, threads = cached
        if time.monotonic() >= expires_at:
            self._thread_picker_cache.pop(int(chat_id), None)
            return None
        return threads

    def _cache_thread_choices(self, chat_id: int, threads: List[dict]) -> List[dict]:
        choices = threads[:THREAD_PICKER_LIMIT]
        self._thread_picker_cache[int(chat_id)] = (
            time.monotonic() + THREAD_PICKER_CACHE_TTL_SECONDS,
            choices,
        )
        return choices

    def _format_thread_choices(
        self,
        threads: List[dict],
        current_thread_id: str,
        *,
        heading: str = "Switchable Threads",
        hint: str = "Use /switch 2 or /switch thread title.",
    ) -> str:
        lines = [f"<b>{escape_html(heading)}</b>"]
        for index, thread in enumerate(threads[:THREAD_PICKER_LIMIT], start=1):
            thread_id = str(thread.get("thread_id") or "")
            title = _thread_title(thread)
            marker = " <i>(current)</i>" if thread_id == current_thread_id else ""
            lines.append(
                f"{index}. {escape_html(title[:80])}{marker}\n"
                f"   <code>{escape_html(thread_id)}</code>"
            )
        if len(threads) > THREAD_PICKER_LIMIT:
            lines.append(f"\n<i>Showing {THREAD_PICKER_LIMIT} of {len(threads)}</i>")
        lines.append(f"\n<i>{escape_html(hint)}</i>")
        return "\n".join(lines)

    # =========================================================================
    # Streaming chat dispatcher
    # =========================================================================

    class _ChatSSEHandler:
        """SSE event handler for Telegram interactive chat.

        Implements :class:`~triggers.sse_consumer.SSEEventHandler` and
        owns the per-stream buffer, progressive-edit, and typing state.
        """

        EDIT_INTERVAL = 1.5  # seconds between message edits

        def __init__(
            self,
            bot: "NymeriaTelegramBot",
            chat_id: int,
            thread_id: str,
            user_id: str,
            telegram_user_id: Optional[int],
            context: ContextTypes.DEFAULT_TYPE,
        ) -> None:
            self._bot = bot
            self._chat_id = chat_id
            self._thread_id = thread_id
            self._user_id = user_id
            self._telegram_user_id = telegram_user_id
            self._context = context

            self._text_buffer = ""
            self._current_msg: Optional[Message] = None
            self._stop_button_msg: Optional[Message] = None
            self._last_edit = 0.0
            self._first_msg_sent = False

            self._typing_task: Optional[asyncio.Task] = asyncio.create_task(
                self._keep_typing()
            )

        def cleanup(self) -> None:
            if self._typing_task:
                self._typing_task.cancel()

        # -- internal helpers -------------------------------------------------

        async def _keep_typing(self) -> None:
            while True:
                try:
                    await self._context.bot.send_chat_action(
                        chat_id=self._chat_id, action=ChatAction.TYPING
                    )
                except Exception:
                    logger.debug("Failed to send typing indicator")
                await asyncio.sleep(4)

        async def flush_text(self, final: bool = False) -> None:
            if not self._text_buffer:
                if final:
                    self._current_msg = None
                return

            raw_chunks = (
                split_message(self._text_buffer, TELEGRAM_SAFE_CHUNK_LENGTH)
                if final
                else [self._text_buffer]
            )

            try:
                for idx, raw_chunk in enumerate(raw_chunks):
                    display = markdown_to_html(raw_chunk)
                    if len(display) > TELEGRAM_TEXT_LIMIT:
                        logger.debug(
                            "Formatted Telegram chunk exceeded %d chars; splitting formatted text",
                            TELEGRAM_TEXT_LIMIT,
                        )
                        display_chunks = split_message(display, TELEGRAM_SAFE_CHUNK_LENGTH)
                    else:
                        display_chunks = [display]

                    for sub_idx, display_chunk in enumerate(display_chunks):
                        is_first_piece = idx == 0 and sub_idx == 0
                        if self._current_msg is not None and is_first_piece:
                            await self._bot._edit_html(self._current_msg, display_chunk)
                            self._last_edit = time.monotonic()
                            continue

                        reply_markup = None
                        if not self._first_msg_sent:
                            stop_token = self._bot._create_stop_button_token(
                                chat_id=self._chat_id,
                                thread_id=self._thread_id,
                                nymeria_user_id=self._user_id,
                                telegram_user_id=self._telegram_user_id,
                            )
                            reply_markup = InlineKeyboardMarkup([[
                                InlineKeyboardButton(
                                    "\u23f9 Stop",
                                    callback_data=f"stop:{stop_token}",
                                )
                            ]])
                        self._current_msg = await self._bot._send_html(
                            self._chat_id, display_chunk, self._context,
                            reply_markup=reply_markup,
                        )
                        if reply_markup is not None:
                            self._stop_button_msg = self._current_msg
                        self._first_msg_sent = True
                        self._last_edit = time.monotonic()
            except Exception:
                logger.warning(
                    "Telegram flush failed for %d chars; retrying as plain chunks",
                    len(self._text_buffer),
                    exc_info=True,
                )
                for raw_chunk in split_message(self._text_buffer, TELEGRAM_SAFE_CHUNK_LENGTH):
                    try:
                        self._current_msg = await self._context.bot.send_message(
                            chat_id=self._chat_id, text=raw_chunk
                        )
                        self._first_msg_sent = True
                        self._last_edit = time.monotonic()
                    except Exception:
                        logger.warning(
                            "Telegram plain chunk send failed (%d chars)",
                            len(raw_chunk),
                            exc_info=True,
                        )

            if final:
                button_msg = self._stop_button_msg or self._current_msg
                if button_msg:
                    try:
                        await button_msg.edit_reply_markup(reply_markup=None)
                    except Exception:
                        logger.debug("Failed to remove stop button from message")
                self._stop_button_msg = None
                self._text_buffer = ""
                self._current_msg = None

        # -- SSEEventHandler callbacks ----------------------------------------

        async def on_thinking(self) -> None:
            pass  # typing indicator already running via _keep_typing

        async def on_response_chunk(self, content: str) -> None:
            self._text_buffer += content
            if len(self._text_buffer) > TELEGRAM_SAFE_CHUNK_LENGTH:
                await self.flush_text(final=True)
            elif time.monotonic() - self._last_edit >= self.EDIT_INTERVAL:
                await self.flush_text()

        async def on_compacting(self, message: str) -> None:
            try:
                await self._bot._send_html(
                    self._chat_id,
                    f"<i>{escape_html(message)}</i>",
                    self._context,
                )
            except Exception as e:
                logger.warning(f"Failed to send compacting status: {e}")

        async def on_compacted(
            self,
            summary: str,
            messages_removed: int,
            title: str = "Context compacted",
        ) -> None:
            try:
                await self._bot._send_html(
                    self._chat_id,
                    format_compaction_notice_html(
                        summary, messages_removed, title=title,
                    ),
                    self._context,
                )
            except Exception as e:
                logger.warning(f"Failed to send compaction notice: {e}")

        async def on_tool_call(
            self,
            name: str,
            args: Dict[str, Any],
            call_id: str,
            count: int,
        ) -> None:
            await self.flush_text(final=True)
            show_tools = self._bot._show_tool_calls.get(self._chat_id, False)
            if show_tools:
                tool_text = format_tool_call_html(name, args)
                try:
                    await self._bot._send_html(self._chat_id, tool_text, self._context)
                except Exception as e:
                    logger.warning(f"Failed to send tool call: {e}")

        async def on_tool_result(
            self,
            call_id: str,
            result: str,
            attachments: List[str],
        ) -> None:
            show_tools = self._bot._show_tool_calls.get(self._chat_id, False)
            if show_tools:
                result_text = format_tool_result_html(result)
                try:
                    await self._bot._send_html(self._chat_id, result_text, self._context)
                except Exception as e:
                    logger.warning(f"Failed to send tool result: {e}")
            for attach_path in attachments:
                await self._bot._send_file_attachment(
                    self._chat_id, attach_path, self._context,
                )

        async def on_tool_reload(self, tools: List[str], ttl: str) -> None:
            names = ", ".join(tools) if tools else "tools"
            try:
                await self._bot._send_html(
                    self._chat_id,
                    f"<i>⚙️ Tool Binding: <b>{names}</b> ({ttl})</i>",
                    self._context,
                )
            except Exception as e:
                logger.warning(f"Failed to send tool reload message: {e}")

        async def on_workspace_artifact(self, path: str) -> None:
            await self._bot._send_file_attachment(
                self._chat_id, path, self._context,
            )

        async def on_error(self, content: str) -> None:
            try:
                await self._context.bot.send_message(
                    chat_id=self._chat_id,
                    text=f"Sorry, I encountered an error: {content}",
                )
            except Exception:
                logger.warning("Failed to send error notification to Telegram", exc_info=True)

        async def on_iteration_limit(self, content: str) -> None:
            try:
                await self._context.bot.send_message(
                    chat_id=self._chat_id, text=f"⚠️ {content}"
                )
            except Exception:
                logger.warning("Failed to send iteration-limit warning to Telegram", exc_info=True)

        async def on_done(self, tool_call_count: int) -> None:
            if tool_call_count and self._text_buffer:
                self._text_buffer += f"\n\n_Tool calls: {tool_call_count}_"
            elif tool_call_count and self._current_msg:
                try:
                    old_text = self._current_msg.text or ""
                    await self._current_msg.edit_text(
                        text=old_text + f"\n\nTool calls: {tool_call_count}"
                    )
                except Exception:
                    logger.debug("Failed to edit Telegram message with tool-call footer")
            await self.flush_text(final=True)

        async def on_stream_end(self, tool_call_count: int) -> None:
            if self._text_buffer:
                if tool_call_count:
                    self._text_buffer += f"\n\n_Tool calls: {tool_call_count}_"
                await self.flush_text(final=True)

    async def _stream_to_chat(
        self,
        chat_id: int,
        message: str,
        thread_id: str,
        user_id: str,
        context: ContextTypes.DEFAULT_TYPE,
        attachments: Optional[List[Dict[str, Any]]] = None,
        telegram_user_id: Optional[int] = None,
    ) -> None:
        """Stream SSE chat events to a Telegram chat with progressive editing.

        Text segments are sent as separate messages at tool boundaries,
        giving natural visual separation via Telegram's chat bubbles.
        """
        handler = self._ChatSSEHandler(
            self,
            chat_id,
            thread_id,
            user_id,
            telegram_user_id,
            context,
        )
        try:
            await consume_sse_stream(
                self.api.chat_stream(
                    message,
                    thread_id,
                    user_id,
                    attachments=attachments,
                    force_unsupported_attachments=bool(attachments),
                ),
                handler,
            )
        except Exception as e:
            logger.error(f"Streaming failed, falling back to sync: {e}", exc_info=True)
            try:
                data = await self.api.chat(
                    message,
                    thread_id,
                    user_id,
                    attachments=attachments,
                    force_unsupported_attachments=bool(attachments),
                )
                response = data.get("response", "")
                tc = data.get("tool_call_count", 0)
                if tc:
                    response += f"\n\nTool calls: {tc}"
                for chunk in split_message(response, 4096):
                    await context.bot.send_message(chat_id=chat_id, text=chunk)
            except Exception as e2:
                logger.error(f"Sync fallback also failed: {e2}", exc_info=True)
                try:
                    await context.bot.send_message(
                        chat_id=chat_id, text=f"Error: {e2}"
                    )
                except Exception:
                    logger.warning("Failed to send last-resort error notification to Telegram", exc_info=True)
        finally:
            handler.cleanup()


    # =========================================================================
    # Chat Commands
    # =========================================================================

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start — Telegram's default entry point.

        Deep-link payloads (passed by the desktop wizard's ``t.me/<bot>?start=...``
        URLs) get dispatched here:

          * ``/start link_<code>`` — claim a self-service Telegram-account link
            code so the user becomes a recognized Nymeria account on this bot.
          * ``/start bind_<code>`` — claim a thread-bind code, attaching the
            current Telegram chat to a desktop thread.

        With no payload, behave as the original welcome message.
        """
        args = context.args or []
        if args:
            payload = args[0]
            if payload.startswith("link_"):
                await self._handle_link_payload(update, payload[len("link_"):])
                return
            if payload.startswith("bind_"):
                await self._handle_bind_payload(update, payload[len("bind_"):])
                return

        await update.message.reply_text(
            "Hello! I'm <b>Nymeria</b>, your AI assistant.\n\n"
            "Send me a message or use /ask to start chatting.\n"
            "Use /help to see all available commands.",
            parse_mode=ParseMode.HTML,
        )

    async def _handle_link_payload(self, update: Update, code: str) -> None:
        """Claim a self-service platform-link code (from /start link_<code>).

        The code was issued by the desktop wizard; consuming it links the
        invoking Telegram user to the issuing Nymeria account, replacing
        the previously admin-only ``users link-platform`` CLI step.
        """
        tg_user = update.effective_user
        if tg_user is None or update.message is None:
            return
        try:
            result = await self.api.claim_platform_link_code(
                code=code.strip(),
                provider="telegram",
                platform_user_id=str(tg_user.id),
            )
        except httpx.HTTPStatusError as e:
            detail = http_error_detail(e)
            if e.response.status_code == 400:
                await update.message.reply_text(
                    f"That link code is invalid or expired: {detail}\n"
                    "Open the desktop app and try again."
                )
            elif e.response.status_code == 409:
                await update.message.reply_text(
                    "Your Telegram account is already linked to a different "
                    "Nymeria user. Ask an admin to resolve the conflict."
                )
            else:
                await update.message.reply_text(f"Couldn't link account: {detail}")
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("link_payload claim failed")
            await update.message.reply_text(f"Unexpected error: {e}")
            return
        # Invalidate any cached "not linked" entry for this Telegram user so
        # the next message uses the fresh link.
        self._user_resolver.invalidate(tg_user.id)
        await update.message.reply_text(
            f"Linked! Your Telegram account is now connected to Nymeria user "
            f"<b>{result.get('user_id')}</b>.\n\n"
            "You can now use the bot freely, or finish the desktop wizard's "
            "next step to bind a specific thread to a chat.",
            parse_mode=ParseMode.HTML,
        )

    async def _handle_bind_payload(self, update: Update, code: str) -> None:
        """Claim a thread-bind code (from /start bind_<code> or /bind <code>)."""
        await self._do_bind(update, code.strip())

    async def _cmd_bind(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /bind <code>. Attach this Telegram chat to a desktop thread."""
        if update.message is None:
            return
        args = context.args or []
        if not args:
            await update.message.reply_text(
                "Usage: /bind <code>\n\n"
                "Get a code from the desktop app: open Thread Settings → "
                "Chat App → Connect Telegram."
            )
            return
        await self._do_bind(update, args[0].strip())

    async def _do_bind(self, update: Update, code: str) -> None:
        if update.message is None or update.effective_chat is None:
            return
        tg_user = update.effective_user
        if tg_user is None:
            return
        chat_id = update.effective_chat.id
        try:
            if self.is_shared_bot:
                # Shared-bot path: the API verifies the issuing Nymeria user
                # matches the calling Telegram user via platform_identities.
                result = await self.api.claim_thread_bind_code(
                    code=code,
                    provider="telegram",
                    platform_chat_id=str(chat_id),
                    expected_provider_user_id=str(tg_user.id),
                )
            else:
                # User-owned-bot path: the bot's registration is the
                # credential — the API verifies the bind code's issuer
                # matches the bot's owner_user_id, no platform_identity
                # needed. This means the wizard's "paste token → bind"
                # flow works even for users with no Telegram identity
                # linked to their Nymeria account.
                result = await self.api.claim_thread_bind_code_via_bot(
                    code=code,
                    provider="telegram",
                    platform_chat_id=str(chat_id),
                    via_user_telegram_bot_id=int(self.user_telegram_bot_id),
                )
        except httpx.HTTPStatusError as e:
            detail = http_error_detail(e)
            status = e.response.status_code
            if status == 400:
                await update.message.reply_text(
                    f"That bind code is invalid or expired: {detail}\n"
                    "Open the desktop app's Connect Telegram wizard and try again."
                )
            elif status == 403:
                await update.message.reply_text(
                    "That bind code was issued by a different Nymeria account. "
                    "Make sure you're using the code from your own desktop app."
                )
            elif status == 409:
                await update.message.reply_text(
                    f"Already bound: {detail}\n"
                    "Use /unbind first if you want to bind a different thread."
                )
            else:
                await update.message.reply_text(f"Couldn't bind: {detail}")
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("bind claim failed")
            await update.message.reply_text(f"Unexpected error: {e}")
            return
        thread_id = result.get("thread_id", "?")
        # Update local cache immediately so the very next message routes to
        # the bound thread without waiting for the periodic refresh.
        self._bindings[int(chat_id)] = thread_id
        self._reverse_bindings[thread_id] = int(chat_id)
        await update.message.reply_text(
            f"Bound this chat to thread <code>{thread_id}</code>.\n\n"
            "Messages here now feed into your desktop thread, and replies "
            "stream both ways.",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_threads(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /threads [query]. List threads this chat can switch to."""
        if update.message is None or update.effective_chat is None:
            return
        chat_id = int(update.effective_chat.id)
        user_id = await self._resolve_or_reject_update(update)
        if user_id is None:
            return
        query = self._parse_args(context).strip()
        try:
            threads = _sorted_switchable_threads(await self.api.list_threads(user_id))
        except Exception as e:  # noqa: BLE001
            logger.exception("thread list failed")
            await update.message.reply_text(f"Couldn't list threads: {e}")
            return

        if query:
            needle = _normalize_thread_label(query)
            threads = [
                thread for thread in threads
                if needle in _normalize_thread_label(_thread_title(thread))
                or needle in _normalize_thread_label(thread.get("thread_id"))
            ]

        if not threads:
            suffix = f" matching '{query}'" if query else ""
            await update.message.reply_text(f"No switchable threads{suffix}.")
            return

        self._cache_thread_choices(chat_id, threads)
        await self._send_html(
            chat_id,
            self._format_thread_choices(
                threads,
                self.resolve_thread_id_for_chat(chat_id),
            ),
            context,
        )

    async def _cmd_switch(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /switch <title|number|id>. Move this chat to another thread."""
        if update.message is None or update.effective_chat is None:
            return
        chat_id = int(update.effective_chat.id)
        user_id = await self._resolve_or_reject_update(update)
        if user_id is None:
            return
        query = self._parse_args(context).strip()
        if not query:
            await update.message.reply_text(
                "Usage: /switch <thread title, list number, or thread id>\n"
                "Use /threads to see numbered choices."
            )
            return

        try:
            threads = await self.api.list_threads(user_id)
        except Exception as e:  # noqa: BLE001
            logger.exception("thread list failed for switch")
            await update.message.reply_text(f"Couldn't list threads: {e}")
            return

        match, ambiguous, reason = _find_thread_match(
            threads,
            query,
            self._get_cached_thread_choices(chat_id),
        )
        if ambiguous:
            self._cache_thread_choices(chat_id, ambiguous)
            await self._send_html(
                chat_id,
                self._format_thread_choices(
                    ambiguous,
                    self.resolve_thread_id_for_chat(chat_id),
                    heading="Multiple Matches",
                    hint="Use /switch 1, /switch 2, etc. to choose one.",
                ),
                context,
            )
            return
        if match is None:
            if reason == "number_out_of_range":
                await update.message.reply_text(
                    "That number isn't in the current /threads list."
                )
            else:
                await update.message.reply_text(
                    f"No switchable thread matched '{query}'. Use /threads <query>."
                )
            return

        target_thread_id = str(match.get("thread_id") or "")
        current_thread_id = self.resolve_thread_id_for_chat(chat_id)
        if target_thread_id == current_thread_id:
            await update.message.reply_text(
                f"This chat is already using {_thread_title(match)}."
            )
            return

        try:
            await self.api.switch_chatapp_binding(
                provider="telegram",
                platform_chat_id=str(chat_id),
                thread_id=target_thread_id,
                user_id=user_id,
                user_telegram_bot_id=self.user_telegram_bot_id,
            )
        except httpx.HTTPStatusError as e:
            await update.message.reply_text(f"Couldn't switch: {http_error_detail(e)}")
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("thread switch failed")
            await update.message.reply_text(f"Couldn't switch: {e}")
            return

        self._set_local_binding(chat_id, target_thread_id)
        await self._send_html(
            chat_id,
            f"Switched this chat to <b>{escape_html(_thread_title(match))}</b>.",
            context,
        )

    async def _cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /new [title]. Create and switch to a fresh thread."""
        if update.message is None or update.effective_chat is None:
            return
        chat_id = int(update.effective_chat.id)
        user_id = await self._resolve_or_reject_update(update)
        if user_id is None:
            return
        title = self._parse_args(context).strip()
        thread_id = str(uuid.uuid4())
        try:
            await self.api.claim_thread(thread_id, user_id)
            if title:
                await self.api.update_thread_metadata(thread_id, user_id, title=title)
            await self.api.switch_chatapp_binding(
                provider="telegram",
                platform_chat_id=str(chat_id),
                thread_id=thread_id,
                user_id=user_id,
                user_telegram_bot_id=self.user_telegram_bot_id,
            )
        except httpx.HTTPStatusError as e:
            await update.message.reply_text(f"Couldn't create thread: {http_error_detail(e)}")
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("new thread failed")
            await update.message.reply_text(f"Couldn't create thread: {e}")
            return

        self._set_local_binding(chat_id, thread_id)
        display_title = title or "New Chat"
        await self._send_html(
            chat_id,
            f"Started a fresh thread: <b>{escape_html(display_title)}</b>.\n"
            f"<code>{escape_html(thread_id)}</code>",
            context,
        )

    async def _cmd_unbind(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /unbind. Remove this chat's thread binding."""
        if update.message is None or update.effective_chat is None:
            return
        chat_id = int(update.effective_chat.id)
        user_id = await self._resolve_or_reject_update(update)
        if user_id is None:
            return
        try:
            result = await self.api.unbind_chatapp_by_chat(
                provider="telegram",
                platform_chat_id=str(chat_id),
                user_id=user_id,
                user_telegram_bot_id=self.user_telegram_bot_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("unbind failed")
            await update.message.reply_text(f"Couldn't unbind: {e}")
            return
        if not result.get("unbound"):
            await update.message.reply_text("This chat isn't bound to a desktop thread.")
            return
        thread_id = result.get("thread_id")
        if isinstance(thread_id, str):
            self._reverse_bindings.pop(thread_id, None)
        self._bindings.pop(chat_id, None)
        await update.message.reply_text(
            "Unbound. Future messages here will use the default Telegram "
            "thread again."
        )

    async def _cmd_ask(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /ask <message>."""
        message_text = self._parse_args(context)
        if not message_text:
            await update.message.reply_text("Usage: /ask <your message>")
            return

        user_id = await self._resolve_or_reject_update(update)
        if user_id is None:
            return

        chat_id = update.effective_chat.id
        thread_id = self.resolve_thread_id_for_chat(chat_id)

        await self._stream_to_chat(
            chat_id=chat_id,
            message=message_text,
            thread_id=thread_id,
            user_id=user_id,
            context=context,
            telegram_user_id=update.effective_user.id if update.effective_user else None,
        )

    async def _cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stop."""
        await self._send_backend_command(update, context, "stop")

    async def _cmd_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /clear."""
        await self._send_backend_command(update, context, "clear")

    async def _cmd_compact(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /compact."""
        chat_id = update.effective_chat.id
        user_id = await self._resolve_or_reject_update(update)
        if user_id is None:
            return
        thread_id = self.resolve_thread_id_for_chat(chat_id)
        try:
            result = await self.api.compact(thread_id, user_id)
            if result.get("success"):
                removed = result.get("messages_removed", 0)
                await update.message.reply_text(
                    f"Compacted: {removed} messages summarized."
                )
            else:
                reason = result.get("reason", "Unknown")
                await update.message.reply_text(f"Compaction skipped: {reason}")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_thread(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /thread."""
        await self._send_backend_command(update, context, "thread")

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status."""
        await self._send_backend_command(update, context, "status")

    async def _cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /model [name] [scope]."""
        await self._send_backend_command(update, context, "model")

    async def _cmd_models(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /models."""
        await self._send_backend_command(update, context, "models")

    async def _cmd_think(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /think [off|on|low|medium|high]."""
        await self._send_backend_command(
            update,
            context,
            "think",
            require_admin=True,
        )

    async def _cmd_context(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /context — detailed context breakdown."""
        await self._send_backend_command(update, context, "context")

    async def _cmd_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /tasks [status]."""
        await self._send_backend_command(update, context, "tasks")

    async def _cmd_export(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /export [markdown|json|txt]."""
        chat_id = update.effective_chat.id
        thread_id = self.resolve_thread_id_for_chat(chat_id)
        user_id = await self._resolve_or_reject_update(update)
        if user_id is None:
            return
        fmt = (context.args[0].lower() if context.args else "markdown")
        if fmt not in ("markdown", "json", "txt"):
            await update.message.reply_text("Usage: /export [markdown|json|txt]")
            return

        try:
            data = await self.api.get_history(thread_id, user_id=user_id)
            messages = data.get("messages", [])
            if not messages:
                await update.message.reply_text("No conversation history to export.")
                return

            if fmt == "json":
                content = _json.dumps(messages, indent=2, ensure_ascii=False)
                ext = "json"
            elif fmt == "txt":
                lines = []
                for msg in messages:
                    role = msg.get("role", "unknown").capitalize()
                    steps = msg.get("steps", [])
                    if steps:
                        lines.append(f"[{role}]")
                        for step in steps:
                            stype = step.get("type", "")
                            if stype == "thinking":
                                lines.append(f"  [Thinking] {step.get('content', '')}")
                            elif stype == "tool_call":
                                name = step.get("name", "?")
                                args = step.get("arguments") or {}
                                result = step.get("result", "")
                                args_str = _json.dumps(args, ensure_ascii=False) if args else ""
                                lines.append(f"  [Tool: {name}] {args_str}")
                                if result:
                                    lines.append(f"    \u2192 {str(result)[:200]}")
                            elif stype == "response":
                                lines.append(step.get("content", ""))
                    else:
                        text = msg.get("content", "")
                        if isinstance(text, list):
                            text = "\n".join(
                                b.get("text", "") for b in text
                                if isinstance(b, dict) and b.get("text")
                            )
                        lines.append(f"[{role}] {text}")
                    lines.append("")
                content = "\n".join(lines)
                ext = "txt"
            else:
                parts = []
                for msg in messages:
                    role = msg.get("role", "unknown").capitalize()
                    steps = msg.get("steps", [])
                    if steps:
                        parts.append(f"### {role}")
                        for step in steps:
                            stype = step.get("type", "")
                            if stype == "thinking":
                                parts.append(f"> *Thinking:* {step.get('content', '')}")
                            elif stype == "tool_call":
                                name = step.get("name", "?")
                                args = step.get("arguments") or {}
                                result = step.get("result", "")
                                parts.append(
                                    f"**Tool: {name}**\n"
                                    f"```json\n{_json.dumps(args, indent=2, ensure_ascii=False)}\n```"
                                )
                                if result:
                                    result_str = str(result)
                                    if len(result_str) > 500:
                                        result_str = result_str[:497] + "..."
                                    parts.append(f"**Result:**\n```\n{result_str}\n```")
                            elif stype == "response":
                                parts.append(step.get("content", ""))
                    else:
                        text = msg.get("content", "")
                        if isinstance(text, list):
                            text = "\n".join(
                                b.get("text", "") for b in text
                                if isinstance(b, dict) and b.get("text")
                            )
                        parts.append(f"### {role}\n\n{text}")
                    parts.append("---")
                content = "\n\n".join(parts)
                ext = "md"

            # Build filename
            chat_name = update.effective_chat.title or "export"
            date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
            filename = f"nymeria-{chat_name}-{date_str}.{ext}"

            encoded = content.encode("utf-8")
            # Telegram 50MB upload limit
            if len(encoded) > 50 * 1024 * 1024:
                await update.message.reply_text(
                    f"Export too large ({len(encoded) / 1024 / 1024:.1f} MB)."
                )
                return

            buf = io.BytesIO(encoded)
            buf.name = filename
            await context.bot.send_document(
                chat_id=chat_id,
                document=buf,
                caption=f"Exported {len(messages)} messages as {filename}",
            )
        except Exception as e:
            logger.error(f"Error exporting: {e}", exc_info=True)
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /restart [bot|api] — admin-gated."""
        # Restart affects every user, so non-admins are rejected even if
        # they're linked Nymeria users.
        user_id = await self._resolve_or_reject_update(update, require_admin=True)
        if user_id is None:
            return

        target = (context.args[0].lower() if context.args else "bot")
        if target == "api":
            # API restart is centralized; suppress transient connection errors
            # raised when the server closes mid-response.
            try:
                result = await self.api.execute_command(
                    "/restart api",
                    source="user",
                    actor="user",
                    surface="telegram",
                    user_id=user_id,
                )
                text = str(result.get("markdown") or "Restarting API server...").strip()
                await update.message.reply_text(text)
            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError):
                pass  # Expected when the API restarts before sending a response.
            except Exception as e:
                await update.message.reply_text(f"Error: {e}")
        else:
            await update.message.reply_text("Restarting bot... (back in a few seconds)")
            logger.info("Bot restart requested via /restart command")
            await self._request_self_restart()

    async def _cmd_showtools(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /showtools — toggle tool call display."""
        chat_id = update.effective_chat.id
        currently_shown = self._show_tool_calls.get(chat_id, False)
        new_state = not currently_shown
        self._show_tool_calls[chat_id] = new_state
        state_str = "shown" if new_state else "hidden"
        desc = (
            "Tool names, arguments, and results will appear as separate messages."
            if new_state else
            "Only response text will be shown, split into separate messages at tool boundaries."
        )
        await update.message.reply_text(f"Tool calls are now {state_str}.\n{desc}")

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help."""
        chat_id = update.effective_chat.id
        user_id = None
        if update.effective_user is not None:
            user_id = await self.resolve_user_id(update.effective_user.id)

        try:
            if user_id:
                commands = await self.api.list_commands(
                    actor="user",
                    surface="telegram",
                    user_id=user_id,
                )
            else:
                commands = _service_command_catalog(is_admin=False)
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not fetch Telegram command catalog: %s", e)
            commands = _service_command_catalog(is_admin=False)

        by_category: dict[str, list[tuple[str, str]]] = {}
        for category, name, description in _merged_telegram_catalog(commands):
            by_category.setdefault(category, []).append((name, description))

        lines = ["<b>Nymeria Bot Commands</b>"]
        for category in sorted(by_category):
            lines.append("")
            lines.append(f"<b>{escape_html(category)}</b>")
            for name, description in by_category[category]:
                desc = escape_html(description.rstrip(".") or "Nymeria command")
                lines.append(f"/{escape_html(name)}: {desc}")
        lines.extend(
            [
                "",
                "<i>You can also send plain text in DMs or reply to me in groups.</i>",
                "<i>Prefix a message with @ThreadTitle or @\"Thread With Spaces\" "
                "to route one turn to another owned thread.</i>",
            ]
        )
        text = "\n".join(lines)
        await self._send_html(chat_id, text, context)

    # =========================================================================
    # TODO Commands
    # =========================================================================

    async def _cmd_todo_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /todo_add <task> [| <schedule>] [| <repeat>] [| <notes>]."""
        raw = self._parse_args(context)
        if not raw:
            await update.message.reply_text(
                "Usage: /todo_add <task> | <schedule> | <repeat> | <notes>\n"
                "Example: /todo_add Check logs | 2h | daily"
            )
            return

        await self._send_backend_command(update, context, "todos add")

    async def _cmd_todo_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /todo_list [active|pending|in_progress|done|all]."""
        await self._send_backend_command(update, context, "todos list")

    async def _cmd_todo_complete(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /todo_complete <id>."""
        todo_id = self._parse_args(context)
        if not todo_id:
            await update.message.reply_text("Usage: /todo_complete <todo_id>")
            return

        await self._send_backend_command(update, context, "todos complete")

    async def _cmd_todo_delete(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /todo_delete <id>."""
        todo_id = self._parse_args(context)
        if not todo_id:
            await update.message.reply_text("Usage: /todo_delete <todo_id>")
            return

        await self._send_backend_command(update, context, "todos delete")

    # =========================================================================
    # Config Commands
    # =========================================================================

    async def _cmd_config_show(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /config_show."""
        await self._send_backend_command(
            update,
            context,
            "config show",
            require_admin=True,
        )

    async def _cmd_config_get(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /config_get <key>."""
        if not self._parse_args(context):
            await update.message.reply_text("Usage: /config_get <key>")
            return
        await self._send_backend_command(
            update,
            context,
            "config get",
            require_admin=True,
        )

    async def _cmd_config_set(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /config_set <key> <value>."""
        if len(context.args or []) < 2:
            await update.message.reply_text("Usage: /config_set <key> <value>")
            return
        await self._send_backend_command(
            update,
            context,
            "config set",
            require_admin=True,
        )

    # =========================================================================
    # Env Commands
    # =========================================================================

    async def _cmd_env_show(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /env_show — show all env vars with masked secrets."""
        await self._send_backend_command(
            update,
            context,
            "env show",
            require_admin=True,
        )

    async def _cmd_env_get(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reject the removed /env_get command if called directly."""
        await update.message.reply_text("Unmasked environment reads are disabled in Telegram.")

    async def _cmd_env_set(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /env_set <key> <value>."""
        if len(context.args or []) < 2:
            await update.message.reply_text("Usage: /env_set <key> <value>")
            return
        await self._send_backend_command(
            update,
            context,
            "env set",
            require_admin=True,
        )

    # =========================================================================
    # Tools Commands
    # =========================================================================

    async def _cmd_tools_core(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /tools_core."""
        await self._send_backend_command(update, context, "tools core")

    async def _cmd_tools_optional(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /tools_optional."""
        await self._send_backend_command(update, context, "tools optional")

    async def _cmd_tools_enabled(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /tools_enabled."""
        await self._send_backend_command(update, context, "tools enabled")

    async def _cmd_tools_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /tools_search <query>."""
        user_id = await self._resolve_or_reject_update(update)
        if user_id is None:
            return
        query = self._parse_args(context)
        if not query:
            await update.message.reply_text("Usage: /tools_search <query>")
            return
        chat_id = update.effective_chat.id
        thread_id = self.resolve_thread_id_for_chat(chat_id)
        try:
            data = await self.api.search_tools(
                query,
                user_id=user_id,
                thread_id=thread_id,
                top_k=8,
            )
            await self._send_html(chat_id, format_tool_search_html(data), context)
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_tools_category(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /tools_category <name>."""
        cat_name = self._parse_args(context)
        if not cat_name:
            await update.message.reply_text("Usage: /tools_category <name>")
            return

        await self._send_backend_command(update, context, "tools category")

    async def _cmd_tools_enable(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /tools_enable <name>."""
        name = self._parse_args(context)
        if not name:
            await update.message.reply_text("Usage: /tools_enable <tool_or_category>")
            return

        await self._send_backend_command(update, context, "tools enable")

    async def _cmd_tools_disable(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /tools_disable <name>."""
        name = self._parse_args(context)
        if not name:
            await update.message.reply_text("Usage: /tools_disable <tool_or_category>")
            return

        await self._send_backend_command(update, context, "tools disable")

    # =========================================================================
    # Memory Commands
    # =========================================================================

    async def _cmd_memory_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /memory_list."""
        await self._send_backend_command(update, context, "memory list")

    async def _cmd_memory_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /memory_save <key> <value>."""
        args = context.args or []
        if len(args) < 2:
            await update.message.reply_text("Usage: /memory_save <key> <value>")
            return
        await self._send_backend_command(update, context, "memory save")

    async def _cmd_memory_forget(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /memory_forget <key>."""
        key = self._parse_args(context)
        if not key:
            await update.message.reply_text("Usage: /memory_forget <key>")
            return
        await self._send_backend_command(update, context, "memory forget")

    async def _cmd_memory_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /memory_search <query>."""
        query = self._parse_args(context)
        if not query:
            await update.message.reply_text("Usage: /memory_search <query>")
            return

        await self._send_backend_command(update, context, "memory search")

    # =========================================================================
    # Notepad Commands
    # =========================================================================

    async def _cmd_notepad_read(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /notepad_read."""
        await self._send_backend_command(update, context, "notepad read")

    async def _cmd_notepad_write(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /notepad_write <content>.

        Appends by default. Use /notepad_write replace:<content> to overwrite.
        """
        raw = self._parse_args(context)
        if not raw:
            await update.message.reply_text("Usage: /notepad_write <content>")
            return

        await self._send_backend_command(update, context, "notepad write")

    async def _cmd_notepad_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /notepad_clear."""
        await self._send_backend_command(update, context, "notepad clear")

    # =========================================================================
    # Callback Query Handlers
    # =========================================================================

    async def _on_stop_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle stop button press during streaming."""
        query = update.callback_query
        if query is None or not isinstance(query.data, str):
            return
        token = query.data.split(":", 1)[1] if ":" in query.data else ""
        self._prune_stop_button_tokens()
        record = self._stop_button_tokens.get(token)
        if record is None:
            await query.answer("That stop button expired.", show_alert=True)
            return

        callback_chat_id = None
        if query.message is not None and query.message.chat is not None:
            callback_chat_id = int(query.message.chat.id)
        elif update.effective_chat is not None:
            callback_chat_id = int(update.effective_chat.id)
        if callback_chat_id != record.chat_id:
            await query.answer("That stop button belongs to another chat.", show_alert=True)
            return

        tg_user = update.effective_user
        if tg_user is None:
            await query.answer("Couldn't verify who pressed the button.", show_alert=True)
            return
        if record.telegram_user_id is not None and int(tg_user.id) != record.telegram_user_id:
            await query.answer("Only the requester can stop this run.", show_alert=True)
            return

        user_id = await self.resolve_user_id(int(tg_user.id))
        if user_id != record.nymeria_user_id:
            await query.answer("Only the requester can stop this run.", show_alert=True)
            return

        try:
            await self.api.stop(record.thread_id, user_id=record.nymeria_user_id)
            self._stop_button_tokens.pop(token, None)
            await query.answer("Abort signal sent.")
        except Exception as e:
            logger.warning(f"Failed to stop thread via button: {e}")
            await query.answer("Couldn't send abort signal.", show_alert=True)

    # =========================================================================
    # Plain Message Handler
    # =========================================================================

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text messages, photos, and document uploads.

        In DMs: respond to all messages.
        In groups: only respond to replies to the bot's messages.

        Photos and documents are downloaded, validated against the same
        MIME / size constraints the desktop frontend enforces, and sent
        to the API as ``attachments``. The message text is taken from the
        message's ``text`` if present, else its ``caption``; if neither
        exists we substitute a placeholder so the API's ``min_length=1``
        check on ``message`` is satisfied.
        """
        if not update.message:
            return

        chat = update.effective_chat
        is_dm = chat.type == "private"

        if not is_dm:
            # In groups, only respond to replies to bot messages
            reply = update.message.reply_to_message
            if not reply or not reply.from_user:
                return
            if reply.from_user.id != context.bot.id:
                return

        chat_id = chat.id
        telegram_user_id = update.effective_user.id
        thread_id = self.resolve_thread_id_for_chat(chat_id)

        nymeria_user_id = await self.resolve_user_id(telegram_user_id)
        if nymeria_user_id is None:
            try:
                await update.message.reply_text(
                    "This Telegram account isn't linked to a Nymeria user yet.\n"
                    "Ask the admin to run: "
                    f"`python run.py users link-platform <email> telegram {telegram_user_id}`"
                )
            except Exception:
                logger.warning("Failed to send account-not-linked message to Telegram", exc_info=True)
            return

        text = (update.message.text or update.message.caption or "").strip()
        attachments, errors = await self._collect_attachments(update, context)

        for err in errors:
            try:
                await context.bot.send_message(chat_id=chat_id, text=err)
            except Exception:
                logger.warning("Failed to send attachment error to Telegram", exc_info=True)

        if not text and not attachments:
            return

        if not text and attachments:
            # API requires non-empty message text — give the agent a hint
            # that the user sent only attachment(s).
            text = "[attachment]" if len(attachments) == 1 else "[attachments]"

        await self._stream_to_chat(
            chat_id=chat_id,
            message=text,
            thread_id=thread_id,
            user_id=nymeria_user_id,
            context=context,
            attachments=attachments or None,
            telegram_user_id=telegram_user_id,
        )

    async def _collect_attachments(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> "tuple[List[Dict[str, Any]], List[str]]":
        """Download and validate attachments from a Telegram message.

        Returns ``(attachments, errors)``. ``attachments`` is the list of
        API-shaped dicts ready to send; ``errors`` is a list of short
        user-facing strings to post back into the chat (oversized files,
        unsupported MIME types, download failures).
        """
        msg = update.message
        attachments: List[Dict[str, Any]] = []
        errors: List[str] = []

        # ---- Photos --------------------------------------------------------
        # Telegram sends a list of PhotoSize objects sorted small → large.
        # The largest version is JPEG-encoded by Telegram regardless of the
        # original upload format.
        if msg.photo:
            best = msg.photo[-1]
            ok, size_err = attachment_helpers.size_within_limit(
                best.file_size, "image/jpeg", "photo.jpg"
            )
            if not ok and size_err:
                errors.append(size_err)
            else:
                try:
                    tg_file = await context.bot.get_file(best.file_id)
                    raw = bytes(await tg_file.download_as_bytearray())
                    att, err = attachment_helpers.build_attachment(
                        raw, "image/jpeg", "photo.jpg"
                    )
                    if att:
                        attachments.append(att)
                    elif err:
                        errors.append(err)
                except Exception as e:
                    logger.warning(f"Failed to download Telegram photo: {e}")
                    errors.append("Couldn't download that photo. Try resending.")

        # ---- Document (image-as-file, PDF, txt, md, csv) -------------------
        if msg.document:
            doc = msg.document
            ok, size_err = attachment_helpers.size_within_limit(
                doc.file_size, doc.mime_type, doc.file_name
            )
            if not ok and size_err:
                errors.append(size_err)
            else:
                try:
                    tg_file = await context.bot.get_file(doc.file_id)
                    raw = bytes(await tg_file.download_as_bytearray())
                    att, err = attachment_helpers.build_attachment(
                        raw, doc.mime_type, doc.file_name
                    )
                    if att:
                        attachments.append(att)
                    elif err:
                        errors.append(err)
                except Exception as e:
                    logger.warning(f"Failed to download Telegram document: {e}")
                    errors.append("Couldn't download that file. Try resending.")

        if len(attachments) > attachment_helpers.MAX_FILES_PER_MESSAGE:
            extra = len(attachments) - attachment_helpers.MAX_FILES_PER_MESSAGE
            attachments = attachments[: attachment_helpers.MAX_FILES_PER_MESSAGE]
            errors.append(
                f"Skipped {extra} extra file(s). Max is "
                f"{attachment_helpers.MAX_FILES_PER_MESSAGE} per message."
            )

        return attachments, errors

    # =========================================================================
    # Autonomous SSE Listener
    # =========================================================================

    async def _api_sse_listener(self) -> None:
        """Background task listening for autonomous task completion events."""
        # Firehose subscription: the service token is admin-role, so we pass
        # `X-Nymeria-Act-As: *` to receive every user's events. The handler
        # filters by thread_id prefix (`telegram_<chat_id>`) to decide which
        # chat to post to.
        url = f"{self.api.base_url}/autonomous/stream"
        headers = {
            "Authorization": f"Bearer {self.api.api_key}",
            "X-Nymeria-Act-As": "*",
        }
        logger.info(f"Telegram SSE listener connecting to {self.api.base_url}/autonomous/stream")

        reconnect_delay = 3
        max_delay = 30

        while True:
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("GET", url, headers=headers) as resp:
                        if resp.status_code != 200:
                            logger.error(f"SSE connection failed: {resp.status_code}")
                            await asyncio.sleep(reconnect_delay)
                            reconnect_delay = min(reconnect_delay * 2, max_delay)
                            continue

                        logger.info("Telegram SSE connected, listening for events")
                        reconnect_delay = 3

                        async for line in resp.aiter_lines():
                            if not line or not line.startswith("data: "):
                                continue
                            raw = line[6:]
                            if raw.startswith(":"):
                                continue
                            try:
                                event = _json.loads(raw)
                            except _json.JSONDecodeError:
                                continue

                            # Multi-bot dispatch: an event for a thread bound
                            # via a user-owned bot must be delivered through
                            # *that* bot's Application (so the message lands
                            # in @YourBot, not @NymeriaaaaaBot). The shared
                            # bot's _handle_sse_event handles the legacy
                            # `telegram_<chat_id>` default and its own
                            # bindings; subordinate bots handle theirs.
                            target = self._dispatch_bot_for_thread(
                                event.get("thread_id", "")
                            )
                            if target is None:
                                continue
                            await target._handle_sse_event(event)

            except httpx.ReadTimeout:
                logger.debug("SSE read timeout, reconnecting...")
            except httpx.ConnectError:
                logger.warning(
                    f"Cannot reach API at {self.api.base_url}, retrying in {reconnect_delay}s"
                )
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error(f"SSE listener error: {e}", exc_info=True)

            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_delay)

    def _dispatch_bot_for_thread(
        self, thread_id: str
    ) -> Optional["NymeriaTelegramBot"]:
        """Pick which bot instance should deliver an SSE event for ``thread_id``.

        The legacy ``telegram_<chat_id>`` default goes through the shared
        bot. A bound thread routes through the bot whose ``_reverse_bindings``
        contains the thread id (filtered per-bot in ``_refresh_bindings``).
        Returns ``None`` for events on threads handled by another integration
        (Discord etc.) — caller drops the event.
        """
        if thread_id.startswith("telegram_"):
            return self
        if thread_id in self._reverse_bindings:
            return self
        for sub in self._user_bots.values():
            if thread_id in sub._reverse_bindings:
                return sub
        return None

    async def _handle_sse_event(self, event: Dict[str, Any]) -> None:
        """Stream an autonomous-task event to the matching Telegram chat.

        Mirrors the per-event splitting that ``_stream_to_chat`` does for
        regular chat: response chunks accumulate in a per-thread buffer and
        get flushed at every tool_call boundary, so preamble text, tool
        announcements, and post-tool replies each land in their own bubble.
        No wrapper header — bubbles look identical to regular chat.

        Two routing cases:
          1. ``thread_id`` starts with ``telegram_`` — the legacy default;
             chat_id is encoded in the suffix.
          2. ``thread_id`` is in ``self._reverse_bindings`` — the user has
             bound a desktop-created thread to a Telegram chat; chat_id
             comes from the binding map.
        Anything else is dispatched by another integration (Discord, etc.),
        so we silently drop it.
        """
        event_type = event.get("type", "")
        thread_id = event.get("thread_id", "")

        chat_id: Optional[int] = None
        if thread_id.startswith("telegram_"):
            try:
                chat_id = int(thread_id[len("telegram_"):])
            except ValueError:
                return
        else:
            chat_id = self.resolve_chat_id_for_thread(thread_id)
            if chat_id is None:
                return

        delivery_mode = await self._get_telegram_autonomous_delivery(thread_id)

        if event_type == "notification":
            if event.get("in_app_only"):
                return
            if delivery_mode == "off":
                return
            message = (
                event.get("message")
                or event.get("summary")
                or event.get("content")
                or ""
            )
            if not str(message).strip():
                return
            for chunk in split_message(markdown_to_html(str(message)), 4000):
                try:
                    await self._send_html(chat_id, chunk)
                except Exception as e:
                    logger.warning(f"Failed to send Telegram notification event: {e}")
            return

        if delivery_mode == "off":
            self._autonomous_state.pop(thread_id, None)
            return

        if delivery_mode == "notify_only":
            if event_type == "task_completed":
                self._autonomous_state.pop(thread_id, None)
                if event.get("error"):
                    err = event.get("content") or "Unknown error"
                    try:
                        await self._send_html(
                            chat_id,
                            f"<i>Autonomous task error:</i> {escape_html(str(err))}",
                        )
                    except Exception as e:
                        logger.warning(f"Failed to send autonomous error: {e}")
            return

        state = self._autonomous_state.get(thread_id)

        def _ensure_state() -> Dict[str, Any]:
            nonlocal state
            if state is None:
                state = {
                    "chat_id": chat_id,
                    "buffer": "",
                    "tool_count": 0,
                    "response_seen": False,
                }
                self._autonomous_state[thread_id] = state
            return state

        async def _flush_buffer(footer: Optional[str] = None) -> None:
            """Send the buffered response text as its own bubble, then reset."""
            if state is None:
                return
            buf = state["buffer"]
            if footer:
                buf = (buf + footer) if buf else footer
            if not buf.strip():
                state["buffer"] = ""
                return
            display = markdown_to_html(buf)
            # Telegram's hard limit is 4096; we already cap response chunks
            # below that, but split as a safety net for the footer case.
            for chunk in split_message(display, 4000):
                try:
                    await self._send_html(chat_id, chunk)
                except Exception as e:
                    logger.warning(f"Failed to send autonomous chunk: {e}")
            state["buffer"] = ""

        try:
            if event_type == "task_started":
                _ensure_state()

            elif event_type == "thinking":
                # Same as regular chat — silently ignored.
                return

            elif event_type == "compacting":
                _ensure_state()
                await _flush_buffer()
                try:
                    status = event.get("message") or "Compacting context..."
                    await self._send_html(chat_id, f"<i>{escape_html(status)}</i>")
                except Exception as e:
                    logger.warning(f"Failed to send autonomous compacting status: {e}")

            elif event_type == "compacted":
                _ensure_state()
                await _flush_buffer()
                try:
                    await self._send_html(
                        chat_id,
                        format_compaction_notice_html(
                            event.get("summary", ""),
                            int(event.get("messages_removed") or 0),
                        ),
                    )
                except Exception as e:
                    logger.warning(f"Failed to send autonomous compaction notice: {e}")

            elif event_type == "context_attached":
                _ensure_state()
                await _flush_buffer()
                try:
                    await self._send_html(
                        chat_id,
                        format_compaction_notice_html(
                            event.get("summary", ""),
                            title="Context summary attached",
                        ),
                    )
                except Exception as e:
                    logger.warning(f"Failed to send autonomous context notice: {e}")

            elif event_type == "response":
                s = _ensure_state()
                chunk = event.get("content", "")
                if not chunk:
                    return
                s["response_seen"] = True
                s["buffer"] += chunk
                # Flush early if a single segment grows large enough that we'd
                # otherwise risk hitting the 4096-char Telegram limit mid-stream.
                if len(s["buffer"]) > 3800:
                    await _flush_buffer()

            elif event_type == "tool_call":
                s = _ensure_state()
                # Finalize preamble text as its own bubble so the tool call
                # marker (if shown) and any post-tool reply land in fresh ones.
                await _flush_buffer()
                s["tool_count"] += 1
                if self._show_tool_calls.get(chat_id, False):
                    tool_text = format_tool_call_html(
                        event.get("name", "?"), event.get("args", {})
                    )
                    try:
                        await self._send_html(chat_id, tool_text)
                    except Exception as e:
                        logger.warning(f"Failed to send autonomous tool call: {e}")

            elif event_type == "tool_result":
                _ensure_state()
                if self._show_tool_calls.get(chat_id, False):
                    result_text = format_tool_result_html(event.get("result", ""))
                    try:
                        await self._send_html(chat_id, result_text)
                    except Exception as e:
                        logger.warning(f"Failed to send autonomous tool result: {e}")
                for attach_path in parse_attach_paths(event.get("result", "")):
                    await self._send_file_attachment(chat_id, attach_path)

            elif event_type == "tool_reload":
                _ensure_state()
                await _flush_buffer()
                tools = event.get("tools") or []
                ttl = event.get("ttl") or ""
                names = ", ".join(str(tool) for tool in tools) if tools else "tools"
                try:
                    await self._send_html(
                        chat_id,
                        (
                            f"<i>Tool Binding: <b>{escape_html(names)}</b>"
                            f"{f' ({escape_html(str(ttl))})' if ttl else ''}</i>"
                        ),
                    )
                except Exception as e:
                    logger.warning(f"Failed to send autonomous tool reload message: {e}")

            elif event_type == "workspace_artifact":
                attach_path = event.get("path")
                if isinstance(attach_path, str) and attach_path:
                    await self._send_file_attachment(chat_id, attach_path)

            elif event_type == "iteration_limit":
                content = event.get("content", "")
                if content:
                    try:
                        await self._send_html(
                            chat_id,
                            f"<i>{escape_html(str(content))}</i>",
                        )
                    except Exception as e:
                        logger.warning(f"Failed to send autonomous iteration notice: {e}")

            elif event_type == "task_completed":
                if event.get("error"):
                    err = event.get("content") or "Unknown error"
                    try:
                        await self._send_html(
                            chat_id,
                            f"<i>Autonomous task error:</i> {escape_html(str(err))}",
                        )
                    except Exception as e:
                        logger.warning(f"Failed to send autonomous error: {e}")
                else:
                    s = _ensure_state()
                    # If we received no per-event responses (older API or
                    # non-streaming task), fall back to the aggregated content.
                    if not s["response_seen"] and not s["buffer"] and not s["tool_count"]:
                        fallback = event.get("content") or ""
                        if fallback:
                            s["buffer"] = fallback
                    footer = (
                        f"\n\n_Tool calls: {s['tool_count']}_"
                        if s["tool_count"]
                        else None
                    )
                    await _flush_buffer(footer=footer)
                self._autonomous_state.pop(thread_id, None)
                logger.info(f"Streamed autonomous result to Telegram chat {chat_id}")

        except Exception as e:
            logger.error(f"Error handling autonomous event {event_type}: {e}", exc_info=True)
            # Drop state so the thread starts fresh on the next task.
            self._autonomous_state.pop(thread_id, None)

    # =========================================================================
    # Error Handler
    # =========================================================================

    async def _error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Global error handler for unhandled exceptions."""
        logger.error("Unhandled exception:", exc_info=context.error)
        if isinstance(context.error, RetryAfter):
            logger.warning(f"Rate limited, retry after {context.error.retry_after}s")
