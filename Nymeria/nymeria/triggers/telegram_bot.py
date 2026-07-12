"""Telegram bot trigger for two-way Nymeria communication.

Thin client architecture: the bot calls the Nymeria REST API for all
operations (chat, tools, memory, etc.) instead of running its own
NymeriaAgent. This mirrors the Discord bot pattern — one agent, one
source of truth.

Supports bot commands, DM responses, reply-to-bot in groups, and SSE
streaming for progressive message editing and autonomous task delivery.
"""

# Keep annotations lazy (PEP 563) so signatures such as
# ``context: ContextTypes.DEFAULT_TYPE`` never evaluate the Telegram SDK at
# import time on a lean install that omits the optional nymeriaos[telegram] extra.
from __future__ import annotations

import asyncio
import io
import logging
import re
import secrets
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Mapping, Optional, Sequence

import httpx

from nymeria.core.thread_classification import NATIVE_PLATFORM_PREFIXES as _NATIVE_SWITCH_THREAD_PREFIXES

from . import attachment_helpers
from .api_client import NymeriaAPIClient
from .bot_helpers import UserResolver, http_error_detail
from .telegram_format import (
    escape_html,
    export_messages_json,
    export_messages_md,
    export_messages_txt,
    format_compaction_notice_html,
    format_tool_call_html,
    format_tool_result_html,
    format_tool_search_html,
    markdown_to_html,
)
from .message_splitter import split_telegram_message as split_message
from .voice_helpers import is_voice_message_mime, strip_markdown_for_speech
from .sse_consumer import (
    consume_autonomous_firehose,
    consume_sse_stream,
    dispatch_event,
    format_auth_prompt_message,
    format_hook_approval_message,
)
from ..core.service_health import HEARTBEAT_INTERVAL_SECONDS, write_service_heartbeat

try:  # pragma: no cover - python-telegram-bot ships in nymeriaos[telegram].
    from telegram import (
        BotCommand,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        Message,
        Update,
    )
    from telegram.constants import ChatAction, ParseMode, ReactionEmoji
    from telegram.error import BadRequest, RetryAfter, TimedOut
    from telegram.ext import (
        AIORateLimiter,
        ApplicationBuilder,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        MessageReactionHandler,
        filters,
    )
except ImportError:  # pragma: no cover - lean installs omit python-telegram-bot.
    # Fallback is typed Any (not None) so type-checking treats these as the
    # imported SDK symbols; at runtime they are None, which SDK_AVAILABLE detects.
    _MISSING: Any = None
    BotCommand = InlineKeyboardButton = InlineKeyboardMarkup = _MISSING
    Message = Update = _MISSING
    ChatAction = ParseMode = ReactionEmoji = _MISSING
    BadRequest = RetryAfter = TimedOut = _MISSING
    AIORateLimiter = ApplicationBuilder = CallbackQueryHandler = _MISSING
    CommandHandler = ContextTypes = MessageHandler = filters = _MISSING
    MessageReactionHandler = _MISSING

#: True when python-telegram-bot is importable. run.py checks this for a friendly error.
SDK_AVAILABLE = Update is not None

logger = logging.getLogger(__name__)


# =============================================================================
# Telegram Formatting Helpers
# =============================================================================
#
# The pure HTML formatters (escape_html, markdown_to_html, format_tool_*_html,
# format_compaction_notice_html) live in ``telegram_format`` and are imported at
# the top of this module, then re-exported here so existing call sites and tests
# keep importing them from ``telegram_bot``.


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
# Hook-approval buttons outlive the backend hold by a margin so a click on a
# just-expired message still gets a clean "no longer pending" answer instead
# of "expired button". The backend window ceiling is 600s.
HOOK_APPROVAL_TOKEN_TTL_SECONDS = 30 * 60

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


@dataclass(frozen=True, slots=True)
class _HookApprovalToken:
    """Server-side state behind an approve/deny inline-keyboard token.

    Telegram callback data is client-visible and capped at 64 bytes, so the
    button carries only an opaque token; the record id and ownership live
    here. ``message_id`` lets the resolved-event handler edit the original
    prompt message (drop the keyboard, show the outcome)."""

    record_id: str
    chat_id: int
    nymeria_user_id: str
    message_id: Optional[int]
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
        # thread_id -> { "handler": _AutonomousSSEHandler } (owns the per-thread
        # response buffer, tool count, and response-seen flag).
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
        # Hook-approval buttons: token -> record state, plus a record_id index
        # so hook_approval_resolved events can edit the original message.
        self._hook_approval_tokens: Dict[str, _HookApprovalToken] = {}
        self._hook_approval_by_record: Dict[str, str] = {}
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
            raw_chat_id = e.get("platform_chat_id")
            if raw_chat_id is None:
                continue
            try:
                chat_id = int(raw_chat_id)
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
        assert app.updater is not None, "Application.updater must be set after build()"
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
        handler: Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, None]],
    ) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, None]]:
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

    def _prune_hook_approval_tokens(self) -> None:
        now = time.monotonic()
        expired = [
            token
            for token, record in self._hook_approval_tokens.items()
            if record.expires_at <= now
        ]
        for token in expired:
            record = self._hook_approval_tokens.pop(token, None)
            if record is not None:
                self._hook_approval_by_record.pop(record.record_id, None)

    def _pop_hook_approval_token(self, token: str) -> Optional[_HookApprovalToken]:
        record = self._hook_approval_tokens.pop(token, None)
        if record is not None:
            self._hook_approval_by_record.pop(record.record_id, None)
        return record

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

        # Lifecycle-hook commands (single /hook token; subcommands ride as args
        # and the backend longest-prefix path match routes them).
        command("hook", self._cmd_hook)

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

        # Callback query handlers (stop button, hook-approval buttons)
        app.add_handler(CallbackQueryHandler(self._on_stop_button, pattern=r"^stop:"))
        app.add_handler(
            CallbackQueryHandler(self._on_hook_approval_button, pattern=r"^hkap:")
        )

        # Plain text messages, photos, document uploads, voice notes, and
        # audio files (DMs and replies-to-bot in groups). Captions on
        # photos/documents are surfaced via update.message.caption inside
        # the handler; voice/audio is transcribed via the backend STT.
        app.add_handler(MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.Document.ALL
             | filters.VOICE | filters.AUDIO) & ~filters.COMMAND,
            self._on_message,
        ))

        # Emoji reactions (message_reaction updates already arrive because
        # polling requests Update.ALL_TYPES). Registered unconditionally; the
        # TELEGRAM_REACTION_TRIGGER_ENABLED toggle gates inside the handler.
        app.add_handler(MessageReactionHandler(
            self._on_message_reaction,
            message_reaction_types=MessageReactionHandler.MESSAGE_REACTION_UPDATED,
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
        if context is not None:
            bot = context.bot
        elif self._application is not None:
            bot = self._application.bot
        else:
            raise RuntimeError("Application not initialized")
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
            _retry_value: Any = e.retry_after
            _retry_secs = _retry_value.total_seconds() if hasattr(_retry_value, "total_seconds") else _retry_value
            await asyncio.sleep(float(_retry_secs))
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
        if context is not None:
            bot = context.bot
        elif self._application is not None:
            bot = self._application.bot
        else:
            raise RuntimeError("Application not initialized")
        buf = io.BytesIO(raw_bytes)
        buf.name = filename
        try:
            if content_type.startswith("image/") and len(raw_bytes) < 10 * 1024 * 1024:
                await bot.send_photo(chat_id=chat_id, photo=buf, caption=filename)
            else:
                await bot.send_document(chat_id=chat_id, document=buf, caption=filename)
            return True
        except RetryAfter as e:
            _retry_value: Any = e.retry_after
            _retry_secs = _retry_value.total_seconds() if hasattr(_retry_value, "total_seconds") else _retry_value
            await asyncio.sleep(float(_retry_secs))
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
        if update.effective_chat is None or update.message is None:
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
            # Full response text across all flushes (no tool-call footers),
            # kept for the optional voice-note reply after the stream ends.
            self._full_response = ""
            # An error event mid-stream means _full_response is a truncated
            # non-answer; the voice reply is skipped in that case.
            self._saw_error = False
            # Set by the reply_suppressed event (the react tool asked to hide
            # this turn's reply text); drops the buffer and the voice reply.
            self._reply_suppressed = False

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
            if self._reply_suppressed:
                self._text_buffer = ""
                if final:
                    await self._remove_stop_button()
                    self._current_msg = None
                return
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
                await self._remove_stop_button()
                self._text_buffer = ""
                self._current_msg = None

        async def _remove_stop_button(self) -> None:
            button_msg = self._stop_button_msg or self._current_msg
            if button_msg:
                try:
                    await button_msg.edit_reply_markup(reply_markup=None)
                except Exception:
                    logger.debug("Failed to remove stop button from message")
            self._stop_button_msg = None

        # -- SSEEventHandler callbacks ----------------------------------------

        async def on_thinking(self) -> None:
            pass  # typing indicator already running via _keep_typing

        async def on_response_chunk(self, content: str) -> None:
            if self._reply_suppressed:
                return
            self._text_buffer += content
            self._full_response += content
            if len(self._text_buffer) > TELEGRAM_SAFE_CHUNK_LENGTH:
                await self.flush_text(final=True)
            elif time.monotonic() - self._last_edit >= self.EDIT_INTERVAL:
                await self.flush_text()

        async def on_reply_suppressed(self) -> None:
            # The react tool asked to hide the reply: drop the pending buffer
            # and ignore any later response text (already-sent bubbles stay).
            self._reply_suppressed = True
            self._text_buffer = ""

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

        async def on_auth_prompt(self, event: Dict[str, Any]) -> None:
            # Own message, never part of the response buffer: a voice reply
            # must not read a one-time credential link aloud.
            try:
                await self._context.bot.send_message(
                    chat_id=self._chat_id, text=format_auth_prompt_message(event)
                )
            except Exception:
                logger.warning("Failed to send auth prompt to Telegram", exc_info=True)

        async def on_error(self, content: str) -> None:
            self._saw_error = True
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
            if self._reply_suppressed:
                await self.flush_text(final=True)
                return
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
            if self._reply_suppressed:
                return
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
        voice_reply: bool = False,
        platform_origin: Optional[Dict[str, Any]] = None,
        is_self_invoke: bool = False,
        trigger_override: Optional[str] = None,
        source: Optional[str] = None,
        source_label: Optional[str] = None,
        publish_autonomous_events: Optional[bool] = None,
    ) -> None:
        """Stream SSE chat events to a Telegram chat with progressive editing.

        Text segments are sent as separate messages at tool boundaries,
        giving natural visual separation via Telegram's chat bubbles.
        ``voice_reply=True`` (set when the user sent a voice message) also
        sends the final response as a voice note, best-effort.

        ``platform_origin`` stamps the turn's originating Telegram message
        for the ``react`` tool; the self-invoke kwargs are set by the
        reaction trigger (``_on_message_reaction``), which dispatches its
        synthetic prompt through this same path with
        ``publish_autonomous_events=False`` so the interactive stream
        rendered here is the turn's only delivery.
        """
        handler = self._ChatSSEHandler(
            self,
            chat_id,
            thread_id,
            user_id,
            telegram_user_id,
            context,
        )
        if trigger_override is None and voice_reply:
            trigger_override = (
                "The user sent this as a voice message; your reply will also "
                "be spoken aloud as a voice note, so keep it conversational."
            )
        streamed_ok = False
        try:
            await consume_sse_stream(
                self.api.chat_stream(
                    message,
                    thread_id,
                    user_id,
                    is_self_invoke=is_self_invoke,
                    trigger_override=trigger_override,
                    attachments=attachments,
                    force_unsupported_attachments=bool(attachments),
                    source=source,
                    source_label=source_label,
                    publish_autonomous_events=publish_autonomous_events,
                    platform_origin=platform_origin,
                ),
                handler,
            )
            streamed_ok = True
        except Exception as e:
            logger.error(f"Streaming failed, falling back to sync: {e}", exc_info=True)
            try:
                data = await self.api.chat(
                    message,
                    thread_id,
                    user_id,
                    trigger_override=trigger_override,
                    attachments=attachments,
                    force_unsupported_attachments=bool(attachments),
                    platform_origin=platform_origin,
                )
                if data.get("suppress_reply"):
                    # The turn's react call hid the reply; the reaction was
                    # delivered via the reaction_request event.
                    return
                response = data.get("response", "")
                tc = data.get("tool_call_count", 0)
                if tc:
                    response += f"\n\nTool calls: {tc}"
                for chunk in split_message(response, 4096):
                    await context.bot.send_message(chat_id=chat_id, text=chunk)
                if voice_reply:
                    await self._send_voice_reply(
                        chat_id, data.get("response", ""), user_id, context
                    )
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
        # Outside the try block: a voice-send hiccup must never trip the
        # sync fallback into re-running the whole turn. Skipped after a
        # mid-stream error event: _full_response would be a truncated
        # non-answer delivered right after an error notice. Also skipped
        # when the react tool suppressed the reply (voice would leak it).
        if (
            voice_reply
            and streamed_ok
            and not handler._saw_error
            and not handler._reply_suppressed
        ):
            await self._send_voice_reply(
                chat_id, handler._full_response, user_id, context
            )

    async def _send_voice_reply(
        self,
        chat_id: int,
        response_text: str,
        user_id: str,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Speak the final response as a Telegram voice note (best-effort).

        The text reply has already been delivered, so every failure here only
        logs: a missing TTS provider (503) must not degrade text chat.
        """
        spoken = strip_markdown_for_speech(response_text or "")
        if not spoken:
            return
        try:
            await context.bot.send_chat_action(
                chat_id=chat_id, action=ChatAction.RECORD_VOICE
            )
        except Exception:
            logger.debug("Failed to send record-voice indicator")
        try:
            audio, content_type = await self.api.synthesize_speech(
                spoken, voice_note=True, user_id=user_id
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 503:
                logger.info("Voice reply skipped: no TTS provider configured")
            else:
                logger.warning(f"Voice reply synthesis failed: {e}")
            return
        except Exception as e:
            logger.warning(f"Voice reply synthesis failed: {e}")
            return
        if not audio:
            return

        mime = (content_type or "").split(";")[0].strip().lower()
        filename = "reply.ogg" if mime in ("audio/ogg", "audio/opus") else "reply.mp3"
        try:
            if is_voice_message_mime(content_type):
                try:
                    await context.bot.send_voice(chat_id=chat_id, voice=audio)
                    return
                except Exception:
                    # Some recipients block voice messages (privacy setting);
                    # an audio file is the closest fallback.
                    logger.info("send_voice failed; retrying as audio file", exc_info=True)
            await context.bot.send_audio(
                chat_id=chat_id, audio=audio, filename=filename, title="Nymeria"
            )
        except Exception as e:
            logger.warning(f"Failed to send voice reply to Telegram: {e}")


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
        if update.message is None:
            return
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
                assert self.user_telegram_bot_id is not None
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
        if update.message is None or update.effective_chat is None:
            return
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
        """Handle /compact [focus instruction]."""
        if update.message is None or update.effective_chat is None:
            return
        chat_id = update.effective_chat.id
        user_id = await self._resolve_or_reject_update(update)
        if user_id is None:
            return
        thread_id = self.resolve_thread_id_for_chat(chat_id)
        focus = " ".join(context.args) if context.args else ""
        compact_message = f"/compact {focus}".strip()
        await self._stream_to_chat(
            chat_id=chat_id,
            message=compact_message,
            thread_id=thread_id,
            user_id=user_id,
            context=context,
            telegram_user_id=update.effective_user.id if update.effective_user else None,
        )

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
        if update.message is None or update.effective_chat is None:
            return
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

            # fmt is validated above, so this lookup cannot KeyError.
            serializers = {
                "json": (export_messages_json, "json"),
                "txt": (export_messages_txt, "txt"),
                "markdown": (export_messages_md, "md"),
            }
            serialize, ext = serializers[fmt]
            content = serialize(messages)

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
        if update.message is None:
            return
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
        if update.message is None or update.effective_chat is None:
            return
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
        if update.effective_chat is None:
            return
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
        if update.message is None:
            return
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
        if update.message is None:
            return
        todo_id = self._parse_args(context)
        if not todo_id:
            await update.message.reply_text("Usage: /todo_complete <todo_id>")
            return

        await self._send_backend_command(update, context, "todos complete")

    async def _cmd_todo_delete(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /todo_delete <id>."""
        if update.message is None:
            return
        todo_id = self._parse_args(context)
        if not todo_id:
            await update.message.reply_text("Usage: /todo_delete <todo_id>")
            return

        await self._send_backend_command(update, context, "todos delete")

    # =========================================================================
    # Lifecycle-hook Commands
    # =========================================================================

    async def _cmd_hook(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /hook [list|create|show|edit|enable|disable|delete|test ...].

        A single ``/hook`` token: the subcommand and its flags arrive as args and
        the backend command dispatcher does the longest-prefix path match, so the
        whole flag grammar (``--event``/``--action``/``--cond``/``--set`` ...) is
        authorable from a Telegram DM exactly as on the CLI.
        """
        await self._send_backend_command(update, context, "hook")

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
        if update.message is None:
            return
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
        if update.message is None:
            return
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
        if update.message is None:
            return
        await update.message.reply_text("Unmasked environment reads are disabled in Telegram.")

    async def _cmd_env_set(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /env_set <key> <value>."""
        if update.message is None:
            return
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
        if update.message is None or update.effective_chat is None:
            return
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
        if update.message is None:
            return
        cat_name = self._parse_args(context)
        if not cat_name:
            await update.message.reply_text("Usage: /tools_category <name>")
            return

        await self._send_backend_command(update, context, "tools category")

    async def _cmd_tools_enable(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /tools_enable <name>."""
        if update.message is None:
            return
        name = self._parse_args(context)
        if not name:
            await update.message.reply_text("Usage: /tools_enable <tool_or_category>")
            return

        await self._send_backend_command(update, context, "tools enable")

    async def _cmd_tools_disable(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /tools_disable <name>."""
        if update.message is None:
            return
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
        if update.message is None:
            return
        args = context.args or []
        if len(args) < 2:
            await update.message.reply_text("Usage: /memory_save <key> <value>")
            return
        await self._send_backend_command(update, context, "memory save")

    async def _cmd_memory_forget(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /memory_forget <key>."""
        if update.message is None:
            return
        key = self._parse_args(context)
        if not key:
            await update.message.reply_text("Usage: /memory_forget <key>")
            return
        await self._send_backend_command(update, context, "memory forget")

    async def _cmd_memory_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /memory_search <query>."""
        if update.message is None:
            return
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
            if update.message is not None:
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
            result = await self.api.stop(record.thread_id, user_id=record.nymeria_user_id)
            self._stop_button_tokens.pop(token, None)
            await query.answer("Abort signal sent.")
        except Exception as e:
            logger.warning(f"Failed to stop thread via button: {e}")
            await query.answer("Couldn't send abort signal.", show_alert=True)
            return

        # Queued user prompts are handed back on stop; echo them so the
        # user can copy/resend (bots have no composer to restore into).
        from ..core.pending_prompt_queue import restored_prompts_notice

        notice = restored_prompts_notice(result.get("restored_prompts") or [])
        if notice:
            try:
                await context.bot.send_message(chat_id=record.chat_id, text=notice)
            except Exception as e:  # noqa: BLE001 - echo is best-effort
                logger.warning(f"Failed to echo restored prompts after stop: {e}")

    # =========================================================================
    # Plain Message Handler
    # =========================================================================

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text messages, photos, documents, voice notes, and audio.

        In DMs: respond to all messages.
        In groups: only respond to replies to the bot's messages.

        Photos and documents are downloaded, validated against the same
        MIME / size constraints the desktop frontend enforces, and sent
        to the API as ``attachments``. Voice notes and audio (including
        audio sent as a document) are transcribed via the backend STT and
        become the message text; the reply is then also spoken back as a
        voice note. The message text is taken from the message's ``text``
        if present, else its ``caption``; if neither exists we substitute
        a placeholder so the API's ``min_length=1`` check on ``message``
        is satisfied.
        """
        if not update.message:
            return

        chat = update.effective_chat
        if chat is None or update.effective_user is None:
            return
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

        # Voice notes / audio files: transcribe via the backend STT and feed
        # the transcript through the normal chat pipeline. A voice message in
        # also means a voice note back (voice-in, voice-out).
        voice_reply = False
        transcript, voice_errors = await self._transcribe_inbound_audio(
            update, context, user_id=nymeria_user_id
        )
        errors.extend(voice_errors)
        if transcript:
            voice_reply = True
            text = f"{text}\n\n{transcript}" if text else transcript

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

        message_id = getattr(update.message, "message_id", None)
        await self._stream_to_chat(
            chat_id=chat_id,
            message=text,
            thread_id=thread_id,
            user_id=nymeria_user_id,
            context=context,
            attachments=attachments or None,
            telegram_user_id=telegram_user_id,
            voice_reply=voice_reply,
            platform_origin={
                "platform": "telegram",
                "channel_id": str(chat_id),
                "message_id": str(message_id),
                "kind": "message",
            } if message_id is not None else None,
        )

    # =========================================================================
    # Emoji-reaction trigger (inbound half of backlog #45)
    # =========================================================================

    @staticmethod
    def _added_reaction_emojis(mr: Any) -> List[str]:
        """Emojis newly added by a ``message_reaction`` update.

        Computed as ``new_reaction - old_reaction`` so a removal (empty delta)
        never fires. Custom/paid reaction types render as ``a custom emoji``
        (their ids are meaningless to the model).
        """

        def _texts(reactions: Any) -> List[str]:
            texts = []
            for r in reactions or []:
                emoji = getattr(r, "emoji", None)
                texts.append(str(emoji) if emoji else "a custom emoji")
            return texts

        old = _texts(getattr(mr, "old_reaction", None))
        added = [e for e in _texts(getattr(mr, "new_reaction", None)) if e not in old]
        return added

    async def _on_message_reaction(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Fire an agent turn when a user adds an emoji reaction in a DM.

        Telegram's ``message_reaction`` update carries only chat + message id
        + emoji (no message text, no author, and bots cannot fetch messages),
        so the Hermes own-message gate is unimplementable here; instead the
        trigger is scoped to PRIVATE chats, where a reaction is always a
        direct signal to the bot. Reaction removals never fire. Gated by
        TELEGRAM_REACTION_TRIGGER_ENABLED (default off).
        """
        from ..config import get_settings

        try:
            if not get_settings().telegram_reaction_trigger_enabled:
                return
        except Exception:  # noqa: BLE001 - settings failure means stay off
            return

        mr = update.message_reaction
        if mr is None or mr.chat is None:
            return
        if getattr(mr.chat, "type", "") != "private":
            return

        # Loop guard (defensive: Telegram does not deliver bot-set reactions,
        # per Bot API docs, but keep the pattern explicit and cheap).
        user = mr.user
        if user is None or getattr(user, "is_bot", False):
            return
        if user.id == context.bot.id:
            return

        added = self._added_reaction_emojis(mr)
        if not added:
            return  # removal or no-op change
        emoji_text = added[0]

        nymeria_user_id = await self.resolve_user_id(user.id)
        if nymeria_user_id is None:
            # A reaction is a one-tap gesture; no onboarding reply spam.
            logger.debug(
                "Reaction trigger: unlinked Telegram user %s ignored", user.id
            )
            return

        chat_id = mr.chat.id
        thread_id = self.resolve_thread_id_for_chat(chat_id)
        reactor = getattr(user, "first_name", None) or "The user"
        prompt = (
            f"[Reaction] {reactor} reacted with {emoji_text} to one of your "
            "recent messages in this chat."
        )
        logger.info(
            "Reaction trigger: %s on message %s -> thread %s",
            emoji_text,
            mr.message_id,
            thread_id,
        )
        await self._stream_to_chat(
            chat_id=chat_id,
            message=prompt,
            thread_id=thread_id,
            user_id=nymeria_user_id,
            context=context,
            telegram_user_id=user.id,
            platform_origin={
                "platform": "telegram",
                "channel_id": str(chat_id),
                "message_id": str(mr.message_id),
                "kind": "reaction",
            },
            is_self_invoke=True,
            trigger_override="reaction",
            source="trigger",
            source_label=f"reaction {emoji_text}",
            publish_autonomous_events=False,
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
        if msg is None:
            return [], []
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
        # Audio documents are handled by the voice path (_transcribe_inbound_audio),
        # so they must not fall through to the image/doc MIME allowlist here.
        if msg.document and not (msg.document.mime_type or "").startswith("audio/"):
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

        return attachment_helpers.finalize_attachments(attachments, errors)

    # Telegram's Bot API refuses getFile downloads above 20 MB, and STT
    # providers cap uploads around 25 MB, so gate at the Bot API limit.
    MAX_VOICE_BYTES = 20 * 1024 * 1024

    async def _transcribe_inbound_audio(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        user_id: str,
    ) -> "tuple[Optional[str], List[str]]":
        """Download and transcribe a voice note or audio file, if present.

        Returns ``(transcript, errors)``. ``transcript`` is ``None`` when the
        message has no audio, the server has no STT configured, or
        transcription failed; ``errors`` carries the user-facing explanation.
        """
        msg = update.message
        if msg is None:
            return None, []

        if msg.voice:
            media = msg.voice
            filename = "voice.ogg"
            mime = media.mime_type or "audio/ogg"
        elif msg.audio:
            media = msg.audio
            filename = msg.audio.file_name or "audio.mp3"
            mime = media.mime_type or "audio/mpeg"
        elif msg.document and (msg.document.mime_type or "").startswith("audio/"):
            # Forwarded/shared audio often arrives as a document.
            media = msg.document
            filename = msg.document.file_name or "audio"
            mime = msg.document.mime_type or "audio/mpeg"
        else:
            return None, []

        if media.file_size and media.file_size > self.MAX_VOICE_BYTES:
            max_mb = self.MAX_VOICE_BYTES // (1024 * 1024)
            return None, [f"That audio is too large to transcribe (max {max_mb} MB)."]

        try:
            await context.bot.send_chat_action(chat_id=msg.chat.id, action=ChatAction.TYPING)
        except Exception:
            logger.debug("Failed to send typing indicator before transcription")

        try:
            tg_file = await context.bot.get_file(media.file_id)
            raw = bytes(await tg_file.download_as_bytearray())
        except Exception as e:
            logger.warning(f"Failed to download Telegram voice/audio: {e}")
            return None, [
                "Couldn't download that audio (Telegram bots can only fetch "
                "files up to 20 MB)."
            ]

        try:
            transcript = await self.api.transcribe_audio(
                raw, filename=filename, content_type=mime, user_id=user_id
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 503:
                return None, [
                    "Voice messages aren't enabled on this server yet: no "
                    "speech-to-text provider is configured (STT_PROVIDER)."
                ]
            logger.warning(f"Voice transcription failed: {e}")
            return None, ["Couldn't transcribe that voice message."]
        except Exception as e:
            logger.warning(f"Voice transcription failed: {e}")
            return None, ["Couldn't transcribe that voice message."]

        transcript = (transcript or "").strip()
        if not transcript:
            return None, ["I couldn't make out any speech in that audio."]
        return transcript, []

    # =========================================================================
    # Autonomous SSE Listener
    # =========================================================================

    async def _handle_firehose_event(self, event: dict) -> None:
        # Multi-bot dispatch: an event for a thread bound via a user-owned bot
        # must be delivered through *that* bot's Application (so the message
        # lands in @YourBot, not @NymeriaaaaaBot). The shared bot's
        # _handle_sse_event handles the legacy `telegram_<chat_id>` default and
        # its own bindings; subordinate bots handle theirs. Returning early
        # (target is None) skips the event, the same as the old loop's continue.
        target = self._dispatch_bot_for_thread(event.get("thread_id", ""))
        if target is None:
            return
        await target._handle_sse_event(event)

    async def _api_sse_listener(self) -> None:
        """Background task listening for autonomous task completion events."""
        # Firehose subscription: the service token is admin-role, so we pass
        # `X-Nymeria-Act-As: *` to receive every user's events. The handler
        # filters by thread_id prefix (`telegram_<chat_id>`) to decide which
        # chat to post to. The reconnect/parse loop is shared with the other
        # bots via consume_autonomous_firehose; omitting should_stop preserves
        # this listener's `while True` / cancellation-only exit.
        await consume_autonomous_firehose(
            base_url=self.api.base_url,
            api_key=self.api.api_key,
            on_event=self._handle_firehose_event,
            log_label="Telegram",
            logger=logger,
        )

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

    class _AutonomousSSEHandler:
        """SSE event handler for Telegram autonomous task delivery.

        Implements :class:`~triggers.sse_consumer.SSEEventHandler` and owns the
        per-thread response buffer, tool count, and response-seen flag. Mirrors
        the per-event splitting that ``_stream_to_chat`` does for regular chat:
        response chunks accumulate in the buffer and flush at every tool_call
        boundary, so preamble text, tool announcements, and post-tool replies
        each land in their own Telegram bubble. No wrapper header, no
        progressive editing, and no stop button: autonomous bubbles look like
        plain chat messages.

        Sends go through ``bot._send_html`` / ``bot._send_file_attachment``
        with no Telegram update context, so they fall back to
        ``bot._application.bot`` (the autonomous firehose has no update).
        """

        def __init__(self, bot: "NymeriaTelegramBot", chat_id: int) -> None:
            self._bot = bot
            self._chat_id = chat_id
            self._text_buffer = ""
            self._tool_count = 0
            self._response_seen = False
            self._reply_suppressed = False

        async def flush_text(self, final: bool = False) -> None:
            """Send the buffered response text as its own bubble, then reset.

            Renders the whole buffer through ``markdown_to_html`` then splits
            the formatted text at 4000 chars (Telegram's hard limit is 4096)
            as a safety net. ``final`` is accepted for the ``SSEEventHandler``
            protocol; the autonomous handler always fully flushes and resets.
            """
            if self._reply_suppressed:
                self._text_buffer = ""
                return
            buf = self._text_buffer
            if not buf.strip():
                self._text_buffer = ""
                return
            display = markdown_to_html(buf)
            for chunk in split_message(display, 4000):
                try:
                    await self._bot._send_html(self._chat_id, chunk)
                except Exception as e:
                    logger.warning(f"Failed to send autonomous chunk: {e}")
            self._text_buffer = ""

        # -- SSEEventHandler callbacks ----------------------------------------

        async def on_thinking(self) -> None:
            pass  # same as regular chat: silently ignored

        async def on_response_chunk(self, content: str) -> None:
            self._response_seen = True
            if self._reply_suppressed:
                return
            self._text_buffer += content
            # Flush early if a single segment grows large enough that we'd
            # otherwise risk hitting the 4096-char Telegram limit mid-stream.
            if len(self._text_buffer) > 3800:
                await self.flush_text()

        async def on_reply_suppressed(self) -> None:
            self._reply_suppressed = True
            self._text_buffer = ""

        async def on_compacting(self, message: str) -> None:
            try:
                await self._bot._send_html(
                    self._chat_id, f"<i>{escape_html(message)}</i>"
                )
            except Exception as e:
                logger.warning(f"Failed to send autonomous compacting status: {e}")

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
                        summary, messages_removed, title=title
                    ),
                )
            except Exception as e:
                logger.warning(f"Failed to send autonomous compaction notice: {e}")

        async def on_tool_call(
            self,
            name: str,
            args: Dict[str, Any],
            call_id: str,
            count: int,
        ) -> None:
            # Finalize preamble text as its own bubble so the tool call marker
            # (if shown) and any post-tool reply land in fresh ones.
            await self.flush_text(final=True)
            self._tool_count = count
            if self._bot._show_tool_calls.get(self._chat_id, False):
                try:
                    await self._bot._send_html(
                        self._chat_id, format_tool_call_html(name, args)
                    )
                except Exception as e:
                    logger.warning(f"Failed to send autonomous tool call: {e}")

        async def on_tool_result(
            self,
            call_id: str,
            result: str,
            attachments: List[str],
        ) -> None:
            if self._bot._show_tool_calls.get(self._chat_id, False):
                try:
                    await self._bot._send_html(
                        self._chat_id, format_tool_result_html(result)
                    )
                except Exception as e:
                    logger.warning(f"Failed to send autonomous tool result: {e}")
            for attach_path in attachments:
                await self._bot._send_file_attachment(self._chat_id, attach_path)

        async def on_tool_reload(self, tools: List[str], ttl: str) -> None:
            # dispatch_event passes the raw event values through, so a
            # present-but-null tools/ttl arrives as None, coerce as the old
            # inline copy did (``event.get("tools") or []``).
            tools = tools or []
            ttl = ttl or ""
            names = ", ".join(str(tool) for tool in tools) if tools else "tools"
            try:
                await self._bot._send_html(
                    self._chat_id,
                    (
                        f"<i>Tool Binding: <b>{escape_html(names)}</b>"
                        f"{f' ({escape_html(str(ttl))})' if ttl else ''}</i>"
                    ),
                )
            except Exception as e:
                logger.warning(f"Failed to send autonomous tool reload message: {e}")

        async def on_workspace_artifact(self, path: str) -> None:
            await self._bot._send_file_attachment(self._chat_id, path)

        async def on_auth_prompt(self, event: Dict[str, Any]) -> None:
            try:
                message = escape_html(format_auth_prompt_message(event)).replace(
                    "\n", "<br>"
                )
                await self._bot._send_html(self._chat_id, message)
            except Exception as e:
                logger.warning(f"Failed to send autonomous auth prompt: {e}")

        async def on_error(self, content: str) -> None:
            try:
                await self._bot._send_html(
                    self._chat_id, f"<i>{escape_html(str(content))}</i>"
                )
            except Exception as e:
                logger.warning(f"Failed to send autonomous error: {e}")

        async def on_iteration_limit(self, content: str) -> None:
            try:
                await self._bot._send_html(
                    self._chat_id, f"<i>{escape_html(str(content))}</i>"
                )
            except Exception as e:
                logger.warning(f"Failed to send autonomous iteration notice: {e}")

        async def on_done(self, tool_call_count: int) -> None:
            pass  # autonomous uses task_completed, not done

        async def on_stream_end(self, tool_call_count: int) -> None:
            pass  # autonomous stream is event-by-event, not consumed as a stream

    async def _on_hook_approval_event(
        self, chat_id: int, event: Dict[str, Any]
    ) -> None:
        """Post an approval prompt with inline Approve/Deny buttons.

        The message body is the shared text fallback (it carries the
        ``/hook approve <id>`` commands, so the hold stays resolvable even if
        the buttons fail). Authorization state lives server-side behind an
        opaque token (Telegram callback data is client-visible, 64-byte cap).
        """
        record_id = str(event.get("record_id") or "")
        owner = str(event.get("user_id") or "")
        if not record_id:
            return
        self._prune_hook_approval_tokens()
        token = secrets.token_urlsafe(16)
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"hkap:a:{token}"),
            InlineKeyboardButton("\U0001f6ab Deny", callback_data=f"hkap:d:{token}"),
        ]])
        text = escape_html(format_hook_approval_message(event))
        message = None
        try:
            message = await self._send_html(chat_id, text, reply_markup=reply_markup)
        except Exception as e:
            logger.warning(f"Failed to send hook approval prompt: {e}")
        message_id = getattr(message, "message_id", None)
        self._hook_approval_tokens[token] = _HookApprovalToken(
            record_id=record_id,
            chat_id=int(chat_id),
            nymeria_user_id=owner,
            message_id=int(message_id) if message_id is not None else None,
            expires_at=time.monotonic() + HOOK_APPROVAL_TOKEN_TTL_SECONDS,
        )
        self._hook_approval_by_record[record_id] = token

    async def _on_hook_approval_resolved_event(self, event: Dict[str, Any]) -> None:
        """Edit the original prompt on resolution: outcome line, no keyboard.

        Fires for every resolution shape (button, /hook command, REST,
        desktop, timeout, abort), so the buttons are always retracted no
        matter where the decision came from.
        """
        record_id = str(event.get("record_id") or "")
        token = self._hook_approval_by_record.get(record_id)
        if token is None:
            return
        record = self._pop_hook_approval_token(token)
        if record is None or record.message_id is None:
            return
        outcome = str(event.get("outcome") or "")
        resolved_by = str(event.get("resolved_by") or "").strip()
        note = str(event.get("note") or "").strip()
        if outcome == "approved":
            line = "✅ Approved" + (f" by {resolved_by}" if resolved_by else "")
        elif outcome == "denied":
            line = "\U0001f6ab Denied" + (f" by {resolved_by}" if resolved_by else "")
        elif outcome == "timeout":
            line = "⏰ No answer in time; the tool call was denied."
        elif outcome == "aborted":
            line = "Turn cancelled; the tool call was denied."
        else:
            line = "No longer pending."
        if note:
            line += f": {escape_html(note)}"
        tool_name = str(event.get("tool_name") or "tool call")
        application = self._application
        if application is None:
            return
        try:
            await application.bot.edit_message_text(
                chat_id=record.chat_id,
                message_id=record.message_id,
                text=f"Approval request for {escape_html(tool_name)}.\n\n{line}",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.debug(f"Failed to edit hook approval message: {e}")

    async def _on_hook_approval_button(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle an Approve/Deny button press.

        The backend is the authorization authority (owner-or-admin via
        Act-As): a 404 means this clicker may not resolve the hold, a 409
        means it is no longer pending. The message edit happens via the
        ``hook_approval_resolved`` firehose event, not here, so every
        resolution surface shares one edit path.
        """
        query = update.callback_query
        if query is None or not isinstance(query.data, str):
            return
        parts = query.data.split(":", 2)
        if len(parts) != 3:
            return
        _, verdict, token = parts
        approved = verdict == "a"
        self._prune_hook_approval_tokens()
        record = self._hook_approval_tokens.get(token)
        if record is None:
            await query.answer("That approval request expired.", show_alert=True)
            return

        callback_chat_id = None
        if query.message is not None and query.message.chat is not None:
            callback_chat_id = int(query.message.chat.id)
        elif update.effective_chat is not None:
            callback_chat_id = int(update.effective_chat.id)
        if callback_chat_id != record.chat_id:
            await query.answer(
                "That approval belongs to another chat.", show_alert=True
            )
            return

        tg_user = update.effective_user
        if tg_user is None:
            await query.answer(
                "Couldn't verify who pressed the button.", show_alert=True
            )
            return
        user_id = await self.resolve_user_id(int(tg_user.id))
        if user_id is None:
            await query.answer(
                "Link your Nymeria account first (/bind).", show_alert=True
            )
            return

        try:
            await self.api.resolve_hook_approval(
                record.record_id, approved, user_id=user_id
            )
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 404:
                await query.answer(
                    "Only the requester or an admin can resolve this.",
                    show_alert=True,
                )
            elif status == 409:
                self._pop_hook_approval_token(token)
                await query.answer("No longer pending.", show_alert=True)
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    logger.debug("Failed to remove approval buttons from message")
            else:
                logger.warning(f"Hook approval resolve failed: {e}")
                await query.answer("Couldn't resolve the approval.", show_alert=True)
            return
        except Exception as e:
            logger.warning(f"Hook approval resolve failed: {e}")
            await query.answer("Couldn't resolve the approval.", show_alert=True)
            return
        await query.answer("Approved." if approved else "Denied.")

    async def _on_reaction_request_event(
        self, chat_id: int, event: Dict[str, Any]
    ) -> None:
        """Execute an outbound ``react`` tool send: set the message reaction.

        Telegram only accepts its standard reaction set from bots, so the
        emoji is validated against ``telegram.constants.ReactionEmoji`` and
        unsupported picks are dropped with a log line. Best-effort like
        notifications: failures log and drop.
        """
        emoji = str(event.get("emoji") or "").strip()
        try:
            target_chat_id = int(event.get("channel_id") or chat_id)
            message_id = int(event.get("message_id") or 0)
        except (TypeError, ValueError):
            return
        if not emoji or not message_id:
            return
        if ReactionEmoji is not None:
            allowed = {e.value for e in ReactionEmoji}
            if emoji not in allowed:
                logger.warning(
                    "Dropping Telegram reaction %r on message %s: not in the "
                    "standard reaction set",
                    emoji,
                    message_id,
                )
                return
        application = self._application
        if application is None:
            return
        try:
            await application.bot.set_message_reaction(
                chat_id=target_chat_id,
                message_id=message_id,
                reaction=emoji,
            )
            logger.info(
                "Posted reaction %s to Telegram message %s", emoji, message_id
            )
        except Exception as e:  # noqa: BLE001 - reaction delivery is best-effort
            logger.warning(
                "Failed to post Telegram reaction %s to message %s: %s",
                emoji,
                message_id,
                e,
            )

    async def _handle_sse_event(self, event: Dict[str, Any]) -> None:
        """Stream an autonomous-task event to the matching Telegram chat.

        Standard SSE event types are routed through the shared
        :func:`~triggers.sse_consumer.dispatch_event` (the same dispatcher the
        interactive ``_ChatSSEHandler`` and the Discord bot use) into a
        per-thread :class:`_AutonomousSSEHandler`. The autonomous-only event
        types (notification, task_started, task_completed) and the
        Telegram-specific delivery-mode gates are handled inline here.

        Two routing cases:
          1. ``thread_id`` starts with ``telegram_`` (the legacy default);
             chat_id is encoded in the suffix.
          2. ``thread_id`` is in ``self._reverse_bindings`` (the user has
             bound a desktop-created thread to a Telegram chat); chat_id
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

        # Approval holds are time-critical and user-authored, so they bypass
        # the autonomous delivery-mode gate: dropping one silently would
        # guarantee a deny-on-timeout.
        if event_type == "hook_approval":
            await self._on_hook_approval_event(chat_id, event)
            return
        if event_type == "hook_approval_resolved":
            await self._on_hook_approval_resolved_event(event)
            return

        # Outbound reaction sends (the react tool) are explicit agent
        # requests like approval prompts, not autonomous transcript delivery,
        # so they also bypass the delivery-mode gates.
        if event_type == "reaction_request":
            if event.get("platform") == "telegram":
                await self._on_reaction_request_event(chat_id, event)
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

        # Full delivery: get-or-create the per-thread handler, then route.
        state = self._autonomous_state.get(thread_id)
        if state is None:
            handler = self._AutonomousSSEHandler(self, chat_id)
            state = {"handler": handler}
            self._autonomous_state[thread_id] = state
        else:
            handler = state["handler"]

        try:
            if event_type == "task_started":
                return

            if event_type == "task_completed":
                if event.get("error"):
                    err = event.get("content") or "Unknown error"
                    try:
                        await self._send_html(
                            chat_id,
                            f"<i>Autonomous task error:</i> {escape_html(str(err))}",
                        )
                    except Exception as e:
                        logger.warning(f"Failed to send autonomous error: {e}")
                elif handler._reply_suppressed:
                    # The turn's react call hid the reply; drop the aggregate
                    # content and footer.
                    pass
                else:
                    # If we received no per-event responses (older API or
                    # non-streaming task), fall back to the aggregated content.
                    if (
                        not handler._response_seen
                        and not handler._text_buffer
                        and not handler._tool_count
                    ):
                        fallback = event.get("content") or ""
                        if fallback:
                            handler._text_buffer = fallback
                    if handler._tool_count:
                        footer = f"\n\n_Tool calls: {handler._tool_count}_"
                        handler._text_buffer = (
                            handler._text_buffer + footer
                            if handler._text_buffer
                            else footer
                        )
                    await handler.flush_text(final=True)
                self._autonomous_state.pop(thread_id, None)
                logger.info(f"Streamed autonomous result to Telegram chat {chat_id}")
                return

            # Standard SSE event types: delegate to the shared dispatcher.
            handler._tool_count = await dispatch_event(
                event, handler, handler._tool_count
            )

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
