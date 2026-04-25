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
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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

from . import attachment_helpers
from .discord_api_client import NymeriaAPIClient

logger = logging.getLogger(__name__)


# =============================================================================
# Formatting Helpers
# =============================================================================


def fmt_tokens(n: int) -> str:
    """Format token count: 5353 -> '5.4k', 1000000 -> '1.0M'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def context_bar(usage_pct: float, width: int = 20) -> str:
    """Render a text progress bar."""
    filled = int(width * usage_pct / 100) if usage_pct else 0
    return "\u2588" * filled + "\u2591" * (width - filled) + f" {usage_pct}%"


def escape_html(text: str) -> str:
    """Escape text for Telegram HTML parse mode."""
    return _html.escape(str(text), quote=False)


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


_ATTACH_RE = re.compile(r"\[attach:(.+?)\]")


def parse_attach_paths(result: str) -> List[str]:
    """Extract file paths from ``[attach:/path]`` tags in a tool result."""
    return _ATTACH_RE.findall(result)


# =============================================================================
# Message Splitting
# =============================================================================


def split_message(content: str, max_length: int = 4096) -> List[str]:
    """Split a message into chunks that fit Telegram's character limit.

    Preserves code blocks, paragraph boundaries, and sentence boundaries.
    """
    if len(content) <= max_length:
        return [content]

    chunks: List[str] = []
    remaining = content

    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        split_at = _find_split_point(remaining, max_length)
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n")

    return [c for c in chunks if c.strip()]


def _find_split_point(text: str, max_length: int) -> int:
    """Find the best split point within max_length characters."""
    # Try not to split inside a code block
    code_block_start = text.rfind("```", 0, max_length)
    if code_block_start > 0:
        count_before = text[:code_block_start].count("```")
        if count_before % 2 == 1:
            closing = text.find("```", code_block_start + 3)
            if closing != -1 and closing + 3 <= len(text):
                end_of_block = closing + 3
                if end_of_block <= max_length:
                    return end_of_block
            block_open = text.rfind("```", 0, code_block_start)
            if block_open > max_length * 0.3:
                return block_open

    para = text.rfind("\n\n", 0, max_length)
    if para > max_length * 0.5:
        return para + 2

    line = text.rfind("\n", 0, max_length)
    if line > max_length * 0.5:
        return line + 1

    sentence = text.rfind(". ", 0, max_length)
    if sentence > max_length * 0.5:
        return sentence + 2

    return max_length


# =============================================================================
# Thread / User ID Helpers
# =============================================================================


def make_thread_id(chat_id: int) -> str:
    """Generate a Nymeria thread ID from a Telegram chat ID."""
    return f"telegram_{chat_id}"


# Platform-identity cache TTL. After 30 minutes a Telegram user's link is
# re-fetched from /platform/resolve, so admin relinks propagate to the bot
# without a restart. See discord_bot.py for the same constant and rationale.
_USER_CACHE_TTL_SECONDS = 30 * 60


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
    ):
        self.api = api
        self.bot_token = bot_token
        self.default_chat_id = default_chat_id
        self._start_time = time.time()
        self._show_tool_calls: Dict[int, bool] = {}  # chat_id -> show
        # Per-thread streaming state for autonomous task delivery.
        # thread_id -> { chat_id, buffer (response text), tool_count }
        self._autonomous_state: Dict[str, Dict[str, Any]] = {}
        self._application = None
        # Telegram user_id -> (Nymeria account user_id or None, expires_at)
        # cache. None (still under TTL) means "checked and confirmed
        # unlinked"; after TTL expiry the entry is re-fetched.
        self._user_cache: Dict[int, tuple[Optional[str], float]] = {}

    async def resolve_user_id(self, telegram_user_id: int) -> Optional[str]:
        """Resolve a Telegram user id to a linked Nymeria account, or None.

        Caches the result (including ``None`` for confirmed-unlinked users)
        for ``_USER_CACHE_TTL_SECONDS`` so admin relinks propagate without
        a restart.
        """
        now = time.monotonic()
        cached = self._user_cache.get(telegram_user_id)
        if cached is not None:
            value, expires_at = cached
            if now < expires_at:
                return value
        try:
            user_id = await self.api.resolve_platform_user("telegram", str(telegram_user_id))
        except Exception as e:  # noqa: BLE001
            logger.warning("resolve_platform_user(telegram, %s) failed: %s", telegram_user_id, e)
            return None
        self._user_cache[telegram_user_id] = (user_id, now + _USER_CACHE_TTL_SECONDS)
        return user_id

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

    def run(self) -> None:
        """Build the Application, register handlers, and start polling."""
        app = (
            ApplicationBuilder()
            .token(self.bot_token)
            .rate_limiter(AIORateLimiter())
            .post_init(self._post_init)
            .build()
        )
        self._application = app
        self._register_handlers(app)
        app.run_polling(allowed_updates=Update.ALL_TYPES)

    async def _post_init(self, application) -> None:
        """Register command menu with Telegram and start SSE listener."""
        commands = [
            BotCommand("ask", "Send a message to Nymeria"),
            BotCommand("stop", "Abort current operation"),
            BotCommand("clear", "Clear conversation history"),
            BotCommand("compact", "Compress conversation context"),
            BotCommand("thread", "Show thread info"),
            BotCommand("status", "System status dashboard"),
            BotCommand("model", "Show or change LLM model"),
            BotCommand("models", "List available models"),
            BotCommand("think", "Set thinking mode"),
            BotCommand("context", "Detailed context breakdown"),
            BotCommand("tasks", "View scheduled tasks"),
            BotCommand("export", "Export conversation history"),
            BotCommand("restart", "Restart bot or API"),
            BotCommand("showtools", "Toggle tool call display"),
            BotCommand("help", "Show all commands"),
            BotCommand("todo_add", "Create a scheduled task"),
            BotCommand("todo_list", "List TODOs"),
            BotCommand("todo_complete", "Mark TODO as done"),
            BotCommand("todo_delete", "Delete a TODO"),
            BotCommand("config_show", "Show all settings"),
            BotCommand("config_get", "Get a setting value"),
            BotCommand("config_set", "Update a setting"),
            BotCommand("env_show", "Show env vars (secrets masked)"),
            BotCommand("env_get", "Get env var (unmasked)"),
            BotCommand("env_set", "Set an env variable"),
            BotCommand("tools_core", "List core tools"),
            BotCommand("tools_optional", "List optional tools"),
            BotCommand("tools_enabled", "List enabled tools"),
            BotCommand("tools_category", "Tools in a category"),
            BotCommand("tools_enable", "Enable a tool or category"),
            BotCommand("tools_disable", "Disable a tool or category"),
            BotCommand("memory_list", "List saved memories"),
            BotCommand("memory_save", "Save a memory"),
            BotCommand("memory_forget", "Remove a memory"),
            BotCommand("memory_search", "Search memories"),
            BotCommand("notepad_read", "Read channel notepad"),
            BotCommand("notepad_write", "Write to notepad"),
            BotCommand("notepad_clear", "Clear notepad"),
        ]
        await application.bot.set_my_commands(commands)
        logger.info(f"Registered {len(commands)} bot commands with Telegram")

        # Start autonomous SSE listener
        asyncio.create_task(self._api_sse_listener())

    def _register_handlers(self, app) -> None:
        """Register all command and message handlers."""
        # Chat commands
        app.add_handler(CommandHandler("ask", self._cmd_ask))
        app.add_handler(CommandHandler("stop", self._cmd_stop))
        app.add_handler(CommandHandler("clear", self._cmd_clear))
        app.add_handler(CommandHandler("compact", self._cmd_compact))
        app.add_handler(CommandHandler("thread", self._cmd_thread))
        app.add_handler(CommandHandler("status", self._cmd_status))
        app.add_handler(CommandHandler("model", self._cmd_model))
        app.add_handler(CommandHandler("models", self._cmd_models))
        app.add_handler(CommandHandler("think", self._cmd_think))
        app.add_handler(CommandHandler("context", self._cmd_context))
        app.add_handler(CommandHandler("tasks", self._cmd_tasks))
        app.add_handler(CommandHandler("export", self._cmd_export))
        app.add_handler(CommandHandler("restart", self._cmd_restart))
        app.add_handler(CommandHandler("showtools", self._cmd_showtools))
        app.add_handler(CommandHandler("help", self._cmd_help))
        app.add_handler(CommandHandler("start", self._cmd_start))

        # TODO commands
        app.add_handler(CommandHandler("todo_add", self._cmd_todo_add))
        app.add_handler(CommandHandler("todo_list", self._cmd_todo_list))
        app.add_handler(CommandHandler("todo_complete", self._cmd_todo_complete))
        app.add_handler(CommandHandler("todo_delete", self._cmd_todo_delete))

        # Config commands
        app.add_handler(CommandHandler("config_show", self._cmd_config_show))
        app.add_handler(CommandHandler("config_get", self._cmd_config_get))
        app.add_handler(CommandHandler("config_set", self._cmd_config_set))

        # Env commands
        app.add_handler(CommandHandler("env_show", self._cmd_env_show))
        app.add_handler(CommandHandler("env_get", self._cmd_env_get))
        app.add_handler(CommandHandler("env_set", self._cmd_env_set))

        # Tools commands
        app.add_handler(CommandHandler("tools_core", self._cmd_tools_core))
        app.add_handler(CommandHandler("tools_optional", self._cmd_tools_optional))
        app.add_handler(CommandHandler("tools_enabled", self._cmd_tools_enabled))
        app.add_handler(CommandHandler("tools_category", self._cmd_tools_category))
        app.add_handler(CommandHandler("tools_enable", self._cmd_tools_enable))
        app.add_handler(CommandHandler("tools_disable", self._cmd_tools_disable))

        # Memory commands
        app.add_handler(CommandHandler("memory_list", self._cmd_memory_list))
        app.add_handler(CommandHandler("memory_save", self._cmd_memory_save))
        app.add_handler(CommandHandler("memory_forget", self._cmd_memory_forget))
        app.add_handler(CommandHandler("memory_search", self._cmd_memory_search))

        # Notepad commands
        app.add_handler(CommandHandler("notepad_read", self._cmd_notepad_read))
        app.add_handler(CommandHandler("notepad_write", self._cmd_notepad_write))
        app.add_handler(CommandHandler("notepad_clear", self._cmd_notepad_clear))

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
                    pass
            # Other BadRequest (message too old, etc.) — ignore
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except TimedOut:
            pass

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

    # =========================================================================
    # Streaming chat dispatcher
    # =========================================================================

    async def _stream_to_chat(
        self,
        chat_id: int,
        message: str,
        thread_id: str,
        user_id: str,
        context: ContextTypes.DEFAULT_TYPE,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Stream SSE chat events to a Telegram chat with progressive editing.

        Text segments are sent as separate messages at tool boundaries,
        giving natural visual separation via Telegram's chat bubbles.
        """
        EDIT_INTERVAL = 1.5

        text_buffer = ""
        current_msg: Optional[Message] = None
        last_edit = 0.0
        tool_call_count = 0
        typing_task: Optional[asyncio.Task] = None
        first_msg_sent = False

        async def _keep_typing():
            """Repeat typing action every 4s (Telegram indicator lasts ~5s)."""
            while True:
                try:
                    await context.bot.send_chat_action(
                        chat_id=chat_id, action=ChatAction.TYPING
                    )
                except Exception:
                    pass
                await asyncio.sleep(4)

        async def _flush(final: bool = False):
            nonlocal text_buffer, current_msg, last_edit, first_msg_sent
            if not text_buffer:
                if final:
                    current_msg = None
                return

            display = markdown_to_html(text_buffer)

            try:
                if current_msg is None:
                    # Attach stop button to first message
                    reply_markup = None
                    if not first_msg_sent:
                        reply_markup = InlineKeyboardMarkup([[
                            InlineKeyboardButton(
                                "\u23f9 Stop", callback_data=f"stop:{thread_id}"
                            )
                        ]])
                    current_msg = await self._send_html(
                        chat_id, display, context, reply_markup=reply_markup
                    )
                    first_msg_sent = True
                    last_edit = time.monotonic()
                else:
                    await self._edit_html(current_msg, display)
                    last_edit = time.monotonic()
            except Exception:
                # Edit/send failed — try sending a new plain message
                try:
                    current_msg = await context.bot.send_message(
                        chat_id=chat_id, text=text_buffer
                    )
                    last_edit = time.monotonic()
                except Exception:
                    pass

            if final:
                # Remove stop button from finalized message
                if current_msg:
                    try:
                        await current_msg.edit_reply_markup(reply_markup=None)
                    except Exception:
                        pass
                text_buffer = ""
                current_msg = None

        # Start typing indicator
        typing_task = asyncio.create_task(_keep_typing())

        try:
            async for event in self.api.chat_stream(
                message,
                thread_id,
                user_id,
                attachments=attachments,
                # Chat clients can't surface the desktop's compatibility
                # modal — auto-accept the risk when the user attached files.
                force_unsupported_attachments=bool(attachments),
            ):
                etype = event.get("type", "")

                if etype == "thinking":
                    pass  # typing indicator already running

                elif etype == "response":
                    chunk = event.get("content", "")
                    if chunk:
                        text_buffer += chunk
                        if len(text_buffer) > 3800:
                            await _flush(final=True)
                        elif time.monotonic() - last_edit >= EDIT_INTERVAL:
                            await _flush()

                elif etype == "tool_call":
                    tool_call_count += 1
                    # Finalize pre-tool text as its own message
                    await _flush(final=True)

                    show_tools = self._show_tool_calls.get(chat_id, False)
                    if show_tools:
                        tool_text = format_tool_call_html(
                            event.get("name", "?"), event.get("args", {})
                        )
                        try:
                            await self._send_html(chat_id, tool_text, context)
                        except Exception as e:
                            logger.warning(f"Failed to send tool call: {e}")

                elif etype == "tool_result":
                    show_tools = self._show_tool_calls.get(chat_id, False)
                    if show_tools:
                        result_text = format_tool_result_html(event.get("result", ""))
                        try:
                            await self._send_html(chat_id, result_text, context)
                        except Exception as e:
                            logger.warning(f"Failed to send tool result: {e}")
                    for attach_path in parse_attach_paths(event.get("result", "")):
                        await self._send_file_attachment(chat_id, attach_path, context)

                elif etype == "tool_reload":
                    await _flush(final=True)
                    tools = event.get("tools", [])
                    ttl = event.get("ttl", "")
                    names = ", ".join(tools) if tools else "tools"
                    try:
                        await self._send_html(
                            chat_id,
                            f"<i>⚙️ Tool Binding: <b>{names}</b> ({ttl})</i>",
                            context,
                        )
                    except Exception as e:
                        logger.warning(f"Failed to send tool reload message: {e}")

                elif etype == "workspace_artifact":
                    attach_path = event.get("path")
                    if isinstance(attach_path, str) and attach_path:
                        await self._send_file_attachment(chat_id, attach_path, context)

                elif etype == "error":
                    await _flush(final=True)
                    error_content = event.get("content", "Unknown error")
                    try:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=f"Sorry, I encountered an error: {error_content}",
                        )
                    except Exception:
                        pass

                elif etype == "iteration_limit":
                    content = event.get("content", "")
                    if content:
                        try:
                            await context.bot.send_message(
                                chat_id=chat_id, text=f"\u26a0\ufe0f {content}"
                            )
                        except Exception:
                            pass

                elif etype == "done":
                    if tool_call_count and text_buffer:
                        text_buffer += f"\n\n_Tool calls: {tool_call_count}_"
                    elif tool_call_count and current_msg:
                        try:
                            old_text = current_msg.text or ""
                            await current_msg.edit_text(
                                text=old_text + f"\n\nTool calls: {tool_call_count}"
                            )
                        except Exception:
                            pass
                    await _flush(final=True)

                # Silently ignore: queued, compacted, context_attached

            # Stream ended — flush any remaining buffer
            if text_buffer:
                if tool_call_count:
                    text_buffer += f"\n\n_Tool calls: {tool_call_count}_"
                await _flush(final=True)

        except Exception as e:
            logger.error(f"Streaming failed, falling back to sync: {e}", exc_info=True)
            # Sync fallback
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
                    pass
        finally:
            if typing_task:
                typing_task.cancel()

    # =========================================================================
    # Chat Commands
    # =========================================================================

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start — Telegram's default entry point."""
        await update.message.reply_text(
            "Hello! I'm <b>Nymeria</b>, your AI assistant.\n\n"
            "Send me a message or use /ask to start chatting.\n"
            "Use /help to see all available commands.",
            parse_mode=ParseMode.HTML,
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
        thread_id = make_thread_id(chat_id)

        await self._stream_to_chat(
            chat_id=chat_id,
            message=message_text,
            thread_id=thread_id,
            user_id=user_id,
            context=context,
        )

    async def _cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stop."""
        chat_id = update.effective_chat.id
        thread_id = make_thread_id(chat_id)
        try:
            await self.api.stop(thread_id)
            await update.message.reply_text("Abort signal sent.")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /clear."""
        chat_id = update.effective_chat.id
        user_id = await self._resolve_or_reject_update(update)
        if user_id is None:
            return
        thread_id = make_thread_id(chat_id)
        try:
            await self.api.clear_thread(thread_id, user_id)
            await update.message.reply_text(
                "Conversation history cleared. Notepad and tool config preserved."
            )
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_compact(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /compact."""
        chat_id = update.effective_chat.id
        user_id = await self._resolve_or_reject_update(update)
        if user_id is None:
            return
        thread_id = make_thread_id(chat_id)
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
        chat_id = update.effective_chat.id
        thread_id = make_thread_id(chat_id)
        try:
            stats = await self.api.get_context_stats(thread_id)
            text = (
                f"<b>Thread Info</b>\n\n"
                f"<b>Thread ID</b>\n<code>{escape_html(thread_id)}</code>\n\n"
                f"<b>Context Usage</b>\n"
                f"{stats.get('usage_percentage', 0)}% "
                f"({stats.get('total_tokens', 0):,} / {stats.get('context_limit', 0):,} tokens)\n\n"
                f"<b>Compactions:</b> {stats.get('compaction_count', 0)}\n"
                f"<b>Context Mode:</b> {stats.get('context_management', 'unknown')}"
            )
            await self._send_html(chat_id, text, context)
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status."""
        chat_id = update.effective_chat.id
        thread_id = make_thread_id(chat_id)
        user_id = await self._resolve_or_reject_update(update)
        if user_id is None:
            return
        try:
            settings, ctx, tools_data, todos = await asyncio.gather(
                self.api.get_settings(),
                self.api.get_context_stats(thread_id),
                self.api.get_default_tools(),
                self.api.list_todos(user_id),
                return_exceptions=True,
            )
            if isinstance(settings, Exception):
                settings = {}
            if isinstance(ctx, Exception):
                ctx = {}
            if isinstance(tools_data, Exception):
                tools_data = {}
            if isinstance(todos, Exception):
                todos = []

            # Uptime
            up = int(time.time() - self._start_time)
            h, rem = divmod(up, 3600)
            m, s = divmod(rem, 60)
            uptime = f"{h}h {m}m" if h else f"{m}m {s}s" if m else f"{s}s"

            # Model
            model = settings.get("llm_model", "?")
            provider = settings.get("llm_provider", "?")
            base_url = settings.get("llm_base_url")
            if base_url and "cli-proxy" in base_url:
                provider = f"{provider} (via CLIProxy)"
            thinking = settings.get("llm_extended_thinking", False)
            effort = settings.get("llm_reasoning_effort")
            think_str = "off"
            if thinking:
                think_str = f"on ({effort})" if effort else "on"

            # Context
            total_tokens = ctx.get("total_tokens", 0)
            context_limit = ctx.get("context_limit", 0)
            usage_pct = ctx.get("usage_percentage", 0)
            compactions = ctx.get("compaction_count", 0)
            ctx_mode = ctx.get("context_management", settings.get("context_management", "?"))

            # Tools
            default_count = len(tools_data.get("default_tools", []))
            available_count = len(tools_data.get("available_tools", []))

            # Tasks
            task_parts = []
            if todos:
                t_pending = sum(1 for t in todos if t.get("status") == "pending")
                t_in_prog = sum(1 for t in todos if t.get("status") == "in_progress")
                if t_pending:
                    task_parts.append(f"{t_pending} pending")
                if t_in_prog:
                    task_parts.append(f"{t_in_prog} in progress")
            tasks_str = " / ".join(task_parts) if task_parts else "none"

            show_tools = self._show_tool_calls.get(chat_id, False)

            text = (
                f"<b>Nymeria Status</b>\n\n"
                f"<b>Model</b>\n"
                f"<code>{escape_html(model)}</code> | {escape_html(provider)} | thinking: {think_str}\n\n"
                f"<b>Context</b>\n"
                f"{context_bar(usage_pct)}\n"
                f"{fmt_tokens(total_tokens)} / {fmt_tokens(context_limit)} tokens\n"
                f"mode: {ctx_mode}"
            )
            if compactions:
                text += f" | {compactions} compaction{'s' if compactions != 1 else ''}"
            text += (
                f"\n\n<b>Tools &amp; System</b>\n"
                f"{default_count} core / {available_count} available\n"
                f"uptime: {uptime}\n\n"
                f"<b>Tasks</b>\n{tasks_str}\n\n"
                f"<b>Telegram</b>\n"
                f"show tools: {'on' if show_tools else 'off'}\n\n"
                f"<i>thread: {escape_html(thread_id)}</i>"
            )
            await self._send_html(chat_id, text, context)
        except Exception as e:
            logger.error(f"Error getting status: {e}", exc_info=True)
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /model [name] [scope]."""
        chat_id = update.effective_chat.id
        args = context.args or []
        thread_id = make_thread_id(chat_id)
        user_id = await self._resolve_or_reject_update(update)
        if user_id is None:
            return
        try:
            if not args:
                settings = await self.api.get_settings()
                tc = await self.api.get_thread_config(thread_id, user_id=user_id)
                llm_cfg = (tc or {}).get("llm_config") or {}
                thread_model = llm_cfg.get("model")
                lines = [f"<b>Global:</b> <code>{escape_html(settings.get('llm_model', '?'))}</code> ({escape_html(settings.get('llm_provider', '?'))})"]
                if thread_model:
                    lines.append(f"<b>This chat:</b> <code>{escape_html(thread_model)}</code> (override)")
                else:
                    lines.append("<b>This chat:</b> using global default")
                await self._send_html(chat_id, "\n".join(lines), context)
            else:
                name = args[0]
                scope = args[1] if len(args) > 1 else "global"
                if scope == "thread":
                    await self.api.update_thread_config(
                        thread_id, user_id=user_id, llm_config={"model": name}
                    )
                    await update.message.reply_text(f"Model for this chat set to {name}.")
                else:
                    # Global model change — admin only.
                    if await self._resolve_or_reject_update(update, require_admin=True) is None:
                        return
                    await self.api.update_settings(llm_model=name)
                    await update.message.reply_text(f"Global model set to {name}.")
        except httpx.HTTPStatusError as e:
            detail = e.response.json().get("detail", str(e)) if e.response else str(e)
            await update.message.reply_text(f"Error: {detail}")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_models(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /models."""
        chat_id = update.effective_chat.id
        try:
            models = await self.api.list_available_models()
            settings = await self.api.get_settings()
            current = settings.get("llm_model", "")

            if not models:
                await update.message.reply_text("No models returned from provider.")
                return

            lines = [
                f"<b>Available Models</b>",
                f"{len(models)} models from {escape_html(settings.get('llm_provider', '?'))}\n",
            ]
            for m in models[:25]:
                model_id = m.get("id") or m.get("name", "?")
                ctx_len = m.get("context_length") or m.get("context_window")
                ctx_str = f" | {fmt_tokens(ctx_len)} ctx" if ctx_len else ""
                marker = " <b>(current)</b>" if model_id == current else ""
                lines.append(f"<code>{escape_html(model_id)}</code>{marker}{ctx_str}")

            if len(models) > 25:
                lines.append(f"\n<i>Showing 25 of {len(models)}</i>")

            await self._send_html(chat_id, "\n".join(lines), context)
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_think(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /think [off|on|low|medium|high]."""
        # /think mutates global settings via update_settings(); admin-only,
        # otherwise any chat member could toggle reasoning effort for every
        # Nymeria user via the bot's admin service token.
        if await self._resolve_or_reject_update(update, require_admin=True) is None:
            return
        args = context.args or []
        try:
            if not args:
                settings = await self.api.get_settings()
                thinking = settings.get("llm_extended_thinking", False)
                effort = settings.get("llm_reasoning_effort")
                if not thinking:
                    status = "off"
                elif effort:
                    status = f"on (effort: {effort})"
                else:
                    status = "on"
                await update.message.reply_text(f"Thinking is currently {status}.")
                return

            value = args[0].lower()
            if value == "off":
                await self.api.update_settings(
                    llm_extended_thinking=False, llm_reasoning_effort=None
                )
                await update.message.reply_text("Thinking disabled.")
            elif value == "on":
                await self.api.update_settings(llm_extended_thinking=True)
                await update.message.reply_text("Thinking enabled.")
            elif value in ("low", "medium", "high"):
                await self.api.update_settings(
                    llm_extended_thinking=True, llm_reasoning_effort=value
                )
                await update.message.reply_text(f"Thinking enabled, effort: {value}.")
            else:
                await update.message.reply_text(
                    "Usage: /think [off|on|low|medium|high]"
                )
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_context(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /context — detailed context breakdown."""
        chat_id = update.effective_chat.id
        thread_id = make_thread_id(chat_id)
        try:
            ctx, thread_cfg, settings, categories, tools_data = await asyncio.gather(
                self.api.get_context_stats(thread_id),
                self.api.get_thread_config(thread_id),
                self.api.get_settings(),
                self.api.get_tool_categories(),
                self.api.get_default_tools(),
                return_exceptions=True,
            )
            if isinstance(ctx, Exception):
                ctx = {}
            if isinstance(thread_cfg, Exception):
                thread_cfg = None
            if isinstance(settings, Exception):
                settings = {}
            if isinstance(categories, Exception):
                categories = {}
            if isinstance(tools_data, Exception):
                tools_data = {}

            # Model
            effective_model = ctx.get("model") or settings.get("llm_model", "?")
            provider = settings.get("llm_provider", "?")
            base_url = settings.get("llm_base_url")
            if base_url and "cli-proxy" in base_url:
                provider = f"{provider} (via CLIProxy)"

            lines = [
                f"<b>Context Breakdown</b>\n",
                f"<b>Model</b>",
                f"<code>{escape_html(effective_model)}</code> | {escape_html(provider)}",
            ]
            if thread_cfg:
                llm_cfg = thread_cfg.get("llm_config") or {}
                thread_model = llm_cfg.get("model")
                if thread_model and thread_model != settings.get("llm_model"):
                    lines.append(f"\u26a0\ufe0f thread override: model=<code>{escape_html(thread_model)}</code>")

            # Context window
            total_tokens = ctx.get("total_tokens", 0)
            context_limit = ctx.get("context_limit", 0)
            usage_pct = ctx.get("usage_percentage", 0)
            compactions = ctx.get("compaction_count", 0)
            ctx_mode = ctx.get("context_management", settings.get("context_management", "?"))

            lines.append(f"\n<b>Context Window</b>")
            lines.append(context_bar(usage_pct))
            token_line = f"{fmt_tokens(total_tokens)} / {fmt_tokens(context_limit)} tokens"
            cumulative = ctx.get("cumulative_tokens", 0)
            if cumulative:
                token_line += f" (cumulative: {fmt_tokens(cumulative)})"
            lines.append(token_line)
            if compactions:
                lines.append(f"{compactions} compaction{'s' if compactions != 1 else ''}")
            mode_str = f"mode: {ctx_mode}"
            threshold = settings.get("compact_threshold")
            if threshold and ctx_mode == "auto_compact":
                mode_str += f" (threshold {int(threshold * 100)}%)"
            lines.append(mode_str)

            # Tools
            default_tools = set(tools_data.get("default_tools", []))
            available_tools = tools_data.get("available_tools", [])
            cats = categories.get("categories", {}) if isinstance(categories, dict) else {}
            disabled = set()
            extra_enabled = set()
            if thread_cfg:
                disabled = set(thread_cfg.get("disabled_tools") or [])
                extra_enabled = set(thread_cfg.get("enabled_tools") or [])
            effective = (default_tools - disabled) | extra_enabled

            lines.append(f"\n<b>Tools</b>")
            lines.append(f"{len(effective)} enabled (of {len(available_tools)} available)")
            cat_parts = []
            for cat_name in sorted(cats.keys()):
                cat_tools = set(cats[cat_name])
                enabled_in_cat = len(cat_tools & effective)
                total_in_cat = len(cat_tools)
                if enabled_in_cat == total_in_cat:
                    cat_parts.append(f"{cat_name}: {total_in_cat}")
                else:
                    cat_parts.append(f"{cat_name}: {enabled_in_cat}/{total_in_cat}")
            if cat_parts:
                # 3 per line
                while cat_parts:
                    chunk = cat_parts[:3]
                    cat_parts = cat_parts[3:]
                    lines.append(" | ".join(chunk))

            # Thread overrides
            lines.append(f"\n<b>Thread Overrides</b>")
            override_lines = []
            if thread_cfg:
                instructions = thread_cfg.get("instructions")
                if instructions:
                    override_lines.append(f"instructions: {len(instructions)} chars")
                if disabled:
                    d_list = ", ".join(sorted(disabled)[:8])
                    if len(disabled) > 8:
                        d_list += f" (+{len(disabled) - 8} more)"
                    override_lines.append(f"disabled: {d_list}")
                if extra_enabled:
                    e_list = ", ".join(sorted(extra_enabled)[:8])
                    if len(extra_enabled) > 8:
                        e_list += f" (+{len(extra_enabled) - 8} more)"
                    override_lines.append(f"enabled: {e_list}")
            if not override_lines:
                override_lines.append("None \u2014 using global defaults")
            lines.extend(override_lines)

            lines.append(f"\n<i>thread: {escape_html(thread_id)}</i>")
            await self._send_html(chat_id, "\n".join(lines), context)
        except Exception as e:
            logger.error(f"Error getting context: {e}", exc_info=True)
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /tasks [status]."""
        chat_id = update.effective_chat.id
        user_id = await self._resolve_or_reject_update(update)
        if user_id is None:
            return
        filter_val = (context.args[0].lower() if context.args else "active")
        try:
            items = await self.api.list_todos(user_id)
            if filter_val == "active":
                items = [i for i in items if i.get("status") != "done"]
            elif filter_val != "all":
                items = [i for i in items if i.get("status") == filter_val]

            if not items:
                await update.message.reply_text(f"No {filter_val} tasks.")
                return

            status_icons = {"pending": "\u23f3", "in_progress": "\u25b6", "done": "\u2705"}
            lines = [f"<b>Scheduled Tasks</b> ({len(items)} {filter_val})\n"]
            for item in items[:15]:
                st = item.get("status", "pending")
                icon = status_icons.get(st, "?")
                task = escape_html(item.get("task", "")[:60])
                detail = []
                scheduled = item.get("scheduled_for")
                if scheduled:
                    detail.append(f"fires: {scheduled[:16]}")
                recurrence = item.get("recurrence")
                if recurrence:
                    detail.append(f"repeat: {recurrence}")
                detail_str = " | ".join(detail) if detail else "no schedule"
                lines.append(f"{icon} {task}\n    {detail_str}")

            if len(items) > 15:
                lines.append(f"\n<i>Showing 15 of {len(items)}</i>")

            await self._send_html(chat_id, "\n".join(lines), context)
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_export(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /export [markdown|json|txt]."""
        chat_id = update.effective_chat.id
        thread_id = make_thread_id(chat_id)
        fmt = (context.args[0].lower() if context.args else "markdown")
        if fmt not in ("markdown", "json", "txt"):
            await update.message.reply_text("Usage: /export [markdown|json|txt]")
            return

        try:
            data = await self.api.get_history(thread_id)
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
        if await self._resolve_or_reject_update(update, require_admin=True) is None:
            return

        target = (context.args[0].lower() if context.args else "bot")
        if target == "api":
            await update.message.reply_text("Restarting API server...")
            try:
                await self.api.restart_api()
            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError):
                pass
            except Exception as e:
                await update.message.reply_text(f"Error: {e}")
        else:
            await update.message.reply_text("Restarting bot... (back in a few seconds)")
            logger.info("Bot restart requested via /restart command")
            os._exit(0)

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
        text = (
            "<b>Nymeria Bot Commands</b>\n\n"
            "<b>Chat</b>\n"
            "/ask &lt;message&gt; — Send a message\n"
            "/stop — Abort current operation\n"
            "/clear — Clear conversation history\n"
            "/compact — Compress context\n"
            "/export [format] — Export history (markdown/json/txt)\n"
            "/restart [bot|api] — Restart a service\n"
            "/showtools — Toggle tool call display\n"
            "/help — This message\n\n"
            "<b>Model &amp; Info</b>\n"
            "/model [name] [scope] — Show/change model\n"
            "/models — List available models\n"
            "/think [off|on|low|medium|high] — Thinking mode\n"
            "/status — System dashboard\n"
            "/thread — Thread info\n"
            "/context — Context breakdown\n"
            "/tasks [status] — Scheduled tasks\n\n"
            "<b>TODOs</b>\n"
            "/todo_add &lt;task&gt; | &lt;schedule&gt; | &lt;repeat&gt;\n"
            "/todo_list [status] — List TODOs\n"
            "/todo_complete &lt;id&gt; — Mark done\n"
            "/todo_delete &lt;id&gt; — Delete\n\n"
            "<b>Config</b>\n"
            "/config_show — Show all settings\n"
            "/config_get &lt;key&gt; — Get a setting\n"
            "/config_set &lt;key&gt; &lt;value&gt; — Update setting\n\n"
            "<b>Environment</b>\n"
            "/env_show — All env vars (secrets masked)\n"
            "/env_get &lt;key&gt; — Get unmasked value\n"
            "/env_set &lt;key&gt; &lt;value&gt; — Set env variable\n\n"
            "<b>Tools</b>\n"
            "/tools_core — Core tools\n"
            "/tools_optional — Optional categories\n"
            "/tools_enabled — Active tools\n"
            "/tools_category &lt;name&gt; — Category tools\n"
            "/tools_enable &lt;name&gt; — Enable tool/category\n"
            "/tools_disable &lt;name&gt; — Disable tool/category\n\n"
            "<b>Memory</b>\n"
            "/memory_list — List memories\n"
            "/memory_save &lt;key&gt; &lt;value&gt;\n"
            "/memory_forget &lt;key&gt;\n"
            "/memory_search &lt;query&gt;\n\n"
            "<b>Notepad</b>\n"
            "/notepad_read — Read notepad\n"
            "/notepad_write &lt;content&gt; — Append to notepad\n"
            "/notepad_clear — Clear notepad\n\n"
            "<i>You can also send plain text in DMs or reply to me in groups.</i>"
        )
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

        parts = [p.strip() for p in raw.split("|")]
        task = parts[0]
        schedule = parts[1] if len(parts) > 1 and parts[1] else "1d"
        recurrence = parts[2] if len(parts) > 2 and parts[2] else None
        notes = parts[3] if len(parts) > 3 and parts[3] else None

        chat_id = update.effective_chat.id
        user_id = await self._resolve_or_reject_update(update)
        if user_id is None:
            return
        thread_id = make_thread_id(chat_id)
        try:
            result = await self.api.add_todo(
                user_id=user_id, task=task, scheduled_for=schedule,
                notes=notes, recurrence=recurrence, thread_id=thread_id,
            )
            todo_id = result.get("id", "")[:8]
            scheduled = result.get("scheduled_for", "")
            lines = [f"Created TODO {todo_id}: {task}"]
            if scheduled:
                lines.append(f"Fires: {scheduled[:16]}")
            if recurrence:
                lines.append(f"Repeats: {recurrence}")
            await update.message.reply_text("\n".join(lines))
        except httpx.HTTPStatusError as e:
            detail = e.response.json().get("detail", str(e)) if e.response else str(e)
            await update.message.reply_text(f"Error: {detail}")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_todo_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /todo_list [active|pending|in_progress|done|all]."""
        chat_id = update.effective_chat.id
        user_id = await self._resolve_or_reject_update(update)
        if user_id is None:
            return
        filter_val = (context.args[0].lower() if context.args else "active")
        try:
            items = await self.api.list_todos(user_id)
            if filter_val == "active":
                items = [i for i in items if i.get("status") != "done"]
            elif filter_val != "all":
                items = [i for i in items if i.get("status") == filter_val]

            if not items:
                await update.message.reply_text(f"No {filter_val} TODOs found.")
                return

            status_icons = {"pending": "\u23f3", "in_progress": "\u25b6", "done": "\u2705"}
            lines = [f"<b>TODOs ({filter_val})</b> — {len(items)} items\n"]
            for item in items[:25]:
                st = item.get("status", "pending")
                icon = status_icons.get(st, "?")
                task = escape_html(item.get("task", "")[:80])
                todo_id = item.get("id", "")[:8]
                parts = [f"ID: <code>{todo_id}</code>"]
                scheduled = item.get("scheduled_for")
                if scheduled:
                    parts.append(f"fires: {scheduled[:16]}")
                recurrence = item.get("recurrence")
                if recurrence:
                    parts.append(f"repeat: {recurrence}")
                lines.append(f"{icon} {task}\n    {' | '.join(parts)}")

            if len(items) > 25:
                lines.append(f"\n<i>Showing 25 of {len(items)}</i>")

            await self._send_html(chat_id, "\n".join(lines), context)
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_todo_complete(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /todo_complete <id>."""
        todo_id = self._parse_args(context)
        if not todo_id:
            await update.message.reply_text("Usage: /todo_complete <todo_id>")
            return

        user_id = await self._resolve_or_reject_update(update)
        if user_id is None:
            return
        try:
            items = await self.api.list_todos(user_id)
            match = next(
                (i for i in items if i.get("id", "").startswith(todo_id)), None
            )
            if not match:
                await update.message.reply_text(f"No TODO found matching {todo_id}.")
                return

            result = await self.api.complete_todo(user_id, match["id"])
            task = match.get("task", "")
            recurrence = result.get("recurrence")
            if recurrence and result.get("status") == "pending":
                next_fire = result.get("scheduled_for", "")[:16]
                await update.message.reply_text(
                    f"Completed: {task}\nRescheduled ({recurrence}): next fire {next_fire}"
                )
            else:
                await update.message.reply_text(f"Completed: {task}")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_todo_delete(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /todo_delete <id>."""
        todo_id = self._parse_args(context)
        if not todo_id:
            await update.message.reply_text("Usage: /todo_delete <todo_id>")
            return

        user_id = await self._resolve_or_reject_update(update)
        if user_id is None:
            return
        try:
            items = await self.api.list_todos(user_id)
            match = next(
                (i for i in items if i.get("id", "").startswith(todo_id)), None
            )
            if not match:
                await update.message.reply_text(f"No TODO found matching {todo_id}.")
                return

            await self.api.delete_todo(user_id, match["id"])
            await update.message.reply_text(f"Deleted: {match.get('task', '')}")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    # =========================================================================
    # Config Commands
    # =========================================================================

    async def _cmd_config_show(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /config_show."""
        # Admin-gated: /config_* and /env_* touch global settings/secrets.
        if await self._resolve_or_reject_update(update, require_admin=True) is None:
            return
        chat_id = update.effective_chat.id
        try:
            settings = await self.api.get_settings()
            effort = settings.get("llm_reasoning_effort")
            base_url = settings.get("llm_base_url")

            lines = [
                "<b>Settings</b>\n",
                "<b>LLM</b>",
                f"provider: <code>{escape_html(settings.get('llm_provider', '?'))}</code>",
                f"model: <code>{escape_html(settings.get('llm_model', '?'))}</code>",
                f"temperature: {settings.get('llm_temperature', '?')}",
                f"thinking: {'on' if settings.get('llm_extended_thinking') else 'off'}",
            ]
            if effort:
                lines.append(f"reasoning effort: {effort}")
            if base_url:
                lines.append(f"base url: <code>{escape_html(base_url)}</code>")

            lines.append(f"\n<b>Context</b>")
            lines.append(f"mode: {settings.get('context_management', '?')}")
            threshold = settings.get("compact_threshold", 0) or 0
            lines.append(f"compact threshold: {int(threshold * 100)}%")
            lines.append(f"keep messages: {settings.get('compact_keep_messages', '?')}")
            compact_model = settings.get("compact_model")
            if compact_model:
                lines.append(f"compact model: <code>{escape_html(compact_model)}</code>")

            lines.append(f"\n<b>System</b>")
            lines.append(f"log level: {settings.get('log_level', '?')}")
            lines.append(f"watchdog: {'on' if settings.get('watchdog_enabled') else 'off'}")
            if settings.get("watchdog_enabled"):
                lines.append(f"watchdog interval: {settings.get('watchdog_interval_minutes', '?')}m")

            await self._send_html(chat_id, "\n".join(lines), context)
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_config_get(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /config_get <key>."""
        # Admin-gated: /config_* and /env_* touch global settings/secrets.
        if await self._resolve_or_reject_update(update, require_admin=True) is None:
            return
        key = self._parse_args(context)
        if not key:
            await update.message.reply_text("Usage: /config_get <key>")
            return
        try:
            settings = await self.api.get_settings()
            if key in settings:
                await update.message.reply_text(f"{key} = {settings[key]}")
            else:
                available = ", ".join(sorted(settings.keys())[:30])
                await update.message.reply_text(f"Unknown setting '{key}'. Available: {available}")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_config_set(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /config_set <key> <value>."""
        # Admin-gated: /config_* and /env_* touch global settings/secrets.
        if await self._resolve_or_reject_update(update, require_admin=True) is None:
            return
        args = context.args or []
        if len(args) < 2:
            await update.message.reply_text("Usage: /config_set <key> <value>")
            return
        key = args[0]
        value_str = " ".join(args[1:])

        # Auto-convert types
        if value_str.lower() in ("true", "false"):
            parsed = value_str.lower() == "true"
        elif value_str.lower() == "none":
            parsed = None
        else:
            try:
                parsed = int(value_str)
            except ValueError:
                try:
                    parsed = float(value_str)
                except ValueError:
                    parsed = value_str

        try:
            result = await self.api.update_settings(**{key: parsed})
            msg = f"{key} set to {parsed}."
            if result.get("restart_required"):
                msg += "\nThis change requires /restart api to take effect."
            await update.message.reply_text(msg)
        except httpx.HTTPStatusError as e:
            detail = e.response.json().get("detail", str(e)) if e.response else str(e)
            await update.message.reply_text(f"Error: {detail}")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    # =========================================================================
    # Tools Commands
    # =========================================================================

    async def _resolve_tool_names(self, name: str):
        """Resolve a name to tool names — could be category or individual tool.

        Returns (tool_names, is_category, category_name, error_msg).
        """
        name_key = name.lower().strip().replace("-", "_")
        cat_data = await self.api.get_tool_categories()
        categories = cat_data.get("categories", {})

        if name_key in categories:
            return (categories[name_key], True, name_key, None)

        data = await self.api.get_default_tools()
        available = data.get("available_tools", [])
        all_names = {t["name"] for t in available}

        if name_key in all_names:
            return ([name_key], False, None, None)

        cat_list = ", ".join(sorted(categories))
        return ([], False, None, f"Unknown tool or category '{name}'. Categories: {cat_list}")

    # =========================================================================
    # Env Commands
    # =========================================================================

    async def _cmd_env_show(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /env_show — show all env vars with masked secrets."""
        # Admin-gated: /config_* and /env_* touch global settings/secrets.
        if await self._resolve_or_reject_update(update, require_admin=True) is None:
            return
        chat_id = update.effective_chat.id
        try:
            data = await self.api.get_env_vars()
            entries = data.get("entries", [])

            by_cat: Dict[str, list] = {}
            for e in entries:
                by_cat.setdefault(e["category"], []).append(e)

            lines = [
                f"<b>Environment Variables</b>",
                f"{len(entries)} variables ({sum(1 for e in entries if e['is_set'])} set)\n",
            ]

            for cat, items in by_cat.items():
                lines.append(f"<b>{escape_html(cat)}</b>")
                for e in items:
                    if e["is_set"]:
                        val = escape_html(str(e["value"]))
                        if e["is_secret"]:
                            lines.append(f"\U0001f512 <code>{e['name']}</code> = <code>{val}</code>")
                        else:
                            lines.append(f"\u2705 <code>{e['name']}</code> = <code>{val}</code>")
                    else:
                        lines.append(f"\u274c <code>{e['name']}</code>")
                lines.append("")

            lines.append("<i>Use /env_get &lt;key&gt; for unmasked values</i>")

            text = "\n".join(lines)
            # Split if too long
            if len(text) > 4000:
                for chunk in split_message(text, 4000):
                    await self._send_html(chat_id, chunk, context)
            else:
                await self._send_html(chat_id, text, context)
        except Exception as e:
            logger.error(f"Error showing env vars: {e}", exc_info=True)
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_env_get(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /env_get <key> — get unmasked value."""
        # Admin-gated: /config_* and /env_* touch global settings/secrets.
        if await self._resolve_or_reject_update(update, require_admin=True) is None:
            return
        key = self._parse_args(context)
        if not key:
            await update.message.reply_text("Usage: /env_get <key>")
            return
        try:
            data = await self.api.get_env_var(key)
            val = data.get("value")
            name = data.get("name", key)
            if val:
                await update.message.reply_text(f"{name} = {val}")
            else:
                await update.message.reply_text(f"{name} is not set.")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                await update.message.reply_text(f"Unknown variable '{key}'.")
            else:
                await update.message.reply_text(f"Error: {e}")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_env_set(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /env_set <key> <value>."""
        # Admin-gated: /config_* and /env_* touch global settings/secrets.
        if await self._resolve_or_reject_update(update, require_admin=True) is None:
            return
        args = context.args or []
        if len(args) < 2:
            await update.message.reply_text("Usage: /env_set <key> <value>")
            return
        key = args[0]
        value_str = " ".join(args[1:])

        # Auto-convert types
        if value_str.lower() in ("true", "false"):
            parsed = value_str.lower() == "true"
        elif value_str.lower() == "none":
            parsed = None
        else:
            try:
                parsed = int(value_str)
            except ValueError:
                try:
                    parsed = float(value_str)
                except ValueError:
                    parsed = value_str

        try:
            result = await self.api.update_settings(**{key: parsed})
            msg = f"{key} set to {parsed}."
            if result.get("restart_required"):
                msg += "\nThis change requires /restart api to take effect."
            await update.message.reply_text(msg)
        except httpx.HTTPStatusError as e:
            detail = e.response.json().get("detail", str(e)) if e.response else str(e)
            await update.message.reply_text(f"Error: {detail}")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    # =========================================================================
    # Tools Commands
    # =========================================================================

    async def _cmd_tools_core(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /tools_core."""
        chat_id = update.effective_chat.id
        try:
            data = await self.api.get_default_tools()
            default_names = set(data.get("default_tools", []))
            available = data.get("available_tools", [])

            lines = [f"<b>Core Tools</b> ({len(default_names)} tools)\n"]
            for t in available:
                if t.get("name") in default_names:
                    desc = (t.get("description") or "").split("\n")[0][:60]
                    lines.append(f"<code>{escape_html(t['name'])}</code> \u2014 {escape_html(desc)}" if desc else f"<code>{escape_html(t['name'])}</code>")

            await self._send_html(chat_id, "\n".join(lines), context)
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_tools_optional(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /tools_optional."""
        chat_id = update.effective_chat.id
        thread_id = make_thread_id(chat_id)
        try:
            data = await self.api.get_default_tools()
            default_names = set(data.get("default_tools", []))
            available = data.get("available_tools", [])
            tc = await self.api.get_thread_config(thread_id)
            thread_extras = set(tc.get("enabled_tools", [])) if tc else set()

            cats: Dict[str, list] = {}
            for t in available:
                if t.get("name") not in default_names:
                    cat = t.get("category", "other")
                    cats.setdefault(cat, []).append(t)

            total = sum(len(v) for v in cats.values())
            lines = [f"<b>Optional Tools</b> ({total} tools, {len(cats)} categories)\n"]
            for cat_name in sorted(cats):
                entries = cats[cat_name]
                active = sum(1 for t in entries if t["name"] in thread_extras)
                tool_names = ", ".join(f"<code>{escape_html(t['name'])}</code>" for t in entries)
                if len(tool_names) > 300:
                    tool_names = tool_names[:297] + "..."
                status = f" ({active} enabled)" if active else ""
                lines.append(f"<b>{escape_html(cat_name)}</b> ({len(entries)}){status}")
                lines.append(tool_names)
                lines.append("")

            await self._send_html(chat_id, "\n".join(lines), context)
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_tools_enabled(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /tools_enabled."""
        chat_id = update.effective_chat.id
        thread_id = make_thread_id(chat_id)
        try:
            data = await self.api.get_default_tools()
            default_names = set(data.get("default_tools", []))
            available = data.get("available_tools", [])
            tc = await self.api.get_thread_config(thread_id)
            thread_extras = set(tc.get("enabled_tools", [])) if tc else set()
            thread_disabled = set(tc.get("disabled_tools", [])) if tc else set()
            all_enabled = (default_names | thread_extras) - thread_disabled

            lines = [f"<b>Enabled Tools</b> ({len(all_enabled)} active)\n"]

            core_active = sorted(n for n in all_enabled if n in default_names)
            lines.append(f"<b>Core ({len(core_active)})</b>")
            lines.append(", ".join(f"<code>{n}</code>" for n in core_active) or "None")

            disabled_core = sorted(thread_disabled & default_names)
            if disabled_core:
                lines.append(f"\n<b>Core \u2014 disabled here ({len(disabled_core)})</b>")
                lines.append(", ".join(f"<s><code>{n}</code></s>" for n in disabled_core))

            optional_active = sorted(n for n in all_enabled if n not in default_names)
            if optional_active:
                avail_by_name = {t["name"]: t for t in available}
                lines.append(f"\n<b>Optional \u2014 enabled ({len(optional_active)})</b>")
                for name in optional_active:
                    t = avail_by_name.get(name, {})
                    desc = (t.get("description") or "").split("\n")[0][:50]
                    lines.append(f"<code>{escape_html(name)}</code> \u2014 {escape_html(desc)}" if desc else f"<code>{escape_html(name)}</code>")
            else:
                lines.append("\n<b>Optional</b>\nNo optional tools enabled.")

            await self._send_html(chat_id, "\n".join(lines), context)
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_tools_category(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /tools_category <name>."""
        cat_name = self._parse_args(context)
        if not cat_name:
            await update.message.reply_text("Usage: /tools_category <name>")
            return

        chat_id = update.effective_chat.id
        thread_id = make_thread_id(chat_id)
        try:
            data = await self.api.get_default_tools()
            default_names = set(data.get("default_tools", []))
            available = data.get("available_tools", [])
            tc = await self.api.get_thread_config(thread_id)
            thread_extras = set(tc.get("enabled_tools", [])) if tc else set()
            thread_disabled = set(tc.get("disabled_tools", [])) if tc else set()
            all_enabled = (default_names | thread_extras) - thread_disabled

            cat_key = cat_name.lower().strip().replace("-", "_")
            cats: Dict[str, list] = {}
            for t in available:
                cat = t.get("category", "other")
                cats.setdefault(cat, []).append(t)

            if cat_key not in cats:
                avail_cats = ", ".join(sorted(cats))
                await update.message.reply_text(
                    f"Unknown category '{cat_name}'. Available: {avail_cats}"
                )
                return

            entries = cats[cat_key]
            lines = [f"<b>Tools: {escape_html(cat_key)}</b> ({len(entries)} tools)\n"]
            for t in entries:
                tool_name = t["name"]
                enabled = tool_name in all_enabled
                is_default = tool_name in default_names
                icon = "\u2705" if enabled else "\u274c"
                desc = (t.get("description") or "").split("\n")[0][:60]
                tag = " (core)" if is_default else ""
                lines.append(f"{icon} <code>{escape_html(tool_name)}</code>{tag}")
                if desc:
                    lines.append(f"    {escape_html(desc)}")

            await self._send_html(chat_id, "\n".join(lines), context)
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_tools_enable(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /tools_enable <name>."""
        user_id = await self._resolve_or_reject_update(update)
        if user_id is None:
            return
        name = self._parse_args(context)
        if not name:
            await update.message.reply_text("Usage: /tools_enable <tool_or_category>")
            return

        chat_id = update.effective_chat.id
        thread_id = make_thread_id(chat_id)
        try:
            tool_names, is_category, cat_name, error = await self._resolve_tool_names(name)
            if error:
                await update.message.reply_text(error)
                return

            tc = await self.api.get_thread_config(thread_id, user_id=user_id)
            current_enabled = set(tc.get("enabled_tools", [])) if tc else set()
            current_disabled = set(tc.get("disabled_tools", [])) if tc else set()
            new_enabled = current_enabled | set(tool_names)
            new_disabled = current_disabled - set(tool_names)

            await self.api.update_thread_config(
                thread_id,
                user_id=user_id,
                enabled_tools=sorted(new_enabled),
                disabled_tools=sorted(new_disabled),
            )

            if is_category:
                await update.message.reply_text(
                    f"Enabled category {cat_name}: {len(tool_names)} tools."
                )
            else:
                await update.message.reply_text(f"Enabled: {tool_names[0]}")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_tools_disable(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /tools_disable <name>."""
        user_id = await self._resolve_or_reject_update(update)
        if user_id is None:
            return
        name = self._parse_args(context)
        if not name:
            await update.message.reply_text("Usage: /tools_disable <tool_or_category>")
            return

        chat_id = update.effective_chat.id
        thread_id = make_thread_id(chat_id)
        try:
            tool_names, is_category, cat_name, error = await self._resolve_tool_names(name)
            if error:
                await update.message.reply_text(error)
                return

            tc = await self.api.get_thread_config(thread_id, user_id=user_id)
            current_enabled = set(tc.get("enabled_tools", [])) if tc else set()
            current_disabled = set(tc.get("disabled_tools", [])) if tc else set()
            new_enabled = current_enabled - set(tool_names)
            new_disabled = current_disabled | set(tool_names)

            await self.api.update_thread_config(
                thread_id,
                user_id=user_id,
                enabled_tools=sorted(new_enabled),
                disabled_tools=sorted(new_disabled),
            )

            if is_category:
                await update.message.reply_text(
                    f"Disabled category {cat_name}: {len(tool_names)} tools."
                )
            else:
                await update.message.reply_text(f"Disabled: {tool_names[0]}")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    # =========================================================================
    # Memory Commands
    # =========================================================================

    async def _cmd_memory_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /memory_list."""
        chat_id = update.effective_chat.id
        user_id = await self._resolve_or_reject_update(update)
        if user_id is None:
            return
        try:
            memories = await self.api.list_memories(user_id)
            if not memories:
                await update.message.reply_text("No memories saved yet.")
                return

            lines = [f"<b>Your Memories</b> ({len(memories)} stored)\n"]
            for mem in memories[:25]:
                value = mem.get("value", "")
                preview = escape_html(value[:200] + "..." if len(value) > 200 else value)
                lines.append(f"<b>{escape_html(mem.get('key', '?'))}</b>")
                lines.append(preview)
                lines.append("")

            if len(memories) > 25:
                lines.append(f"<i>Showing 25 of {len(memories)}</i>")

            await self._send_html(chat_id, "\n".join(lines), context)
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_memory_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /memory_save <key> <value>."""
        args = context.args or []
        if len(args) < 2:
            await update.message.reply_text("Usage: /memory_save <key> <value>")
            return
        key = args[0]
        value = " ".join(args[1:])
        user_id = await self._resolve_or_reject_update(update)
        if user_id is None:
            return
        try:
            await self.api.save_memory(user_id, key, value)
            await update.message.reply_text(f"Saved memory: {key}")
        except httpx.HTTPStatusError as e:
            detail = e.response.json().get("detail", str(e)) if e.response else str(e)
            await update.message.reply_text(f"Error: {detail}")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_memory_forget(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /memory_forget <key>."""
        key = self._parse_args(context)
        if not key:
            await update.message.reply_text("Usage: /memory_forget <key>")
            return
        user_id = await self._resolve_or_reject_update(update)
        if user_id is None:
            return
        try:
            await self.api.forget_memory(user_id, key)
            await update.message.reply_text(f"Forgot memory: {key}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                await update.message.reply_text(f"No memory found with key '{key}'.")
            else:
                await update.message.reply_text(f"Error: {e}")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_memory_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /memory_search <query>."""
        query = self._parse_args(context)
        if not query:
            await update.message.reply_text("Usage: /memory_search <query>")
            return

        chat_id = update.effective_chat.id
        user_id = await self._resolve_or_reject_update(update)
        if user_id is None:
            return
        try:
            results = await self.api.search_memories(user_id, query)
            if not results:
                await update.message.reply_text(f"No memories matching '{query}'.")
                return

            lines = [f"<b>Memory Search: {escape_html(query)}</b> ({len(results)} results)\n"]
            for mem in results[:25]:
                value = mem.get("value", "")
                preview = escape_html(value[:200] + "..." if len(value) > 200 else value)
                lines.append(f"<b>{escape_html(mem.get('key', '?'))}</b>")
                lines.append(preview)
                lines.append("")

            await self._send_html(chat_id, "\n".join(lines), context)
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    # =========================================================================
    # Notepad Commands
    # =========================================================================

    async def _cmd_notepad_read(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /notepad_read."""
        chat_id = update.effective_chat.id
        thread_id = make_thread_id(chat_id)
        try:
            from ..tools.thread_notes import read_notepad
            content = read_notepad(thread_id)
            if content:
                if len(content) > 3800:
                    content = content[:3797] + "..."
                text = (
                    f"<b>Notepad</b>\n\n"
                    f"{escape_html(content)}\n\n"
                    f"<i>{len(content)} chars | thread: {escape_html(thread_id)}</i>"
                )
                await self._send_html(chat_id, text, context)
            else:
                await update.message.reply_text("Notepad is empty.")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_notepad_write(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /notepad_write <content>.

        Appends by default. Use /notepad_write replace:<content> to overwrite.
        """
        raw = self._parse_args(context)
        if not raw:
            await update.message.reply_text("Usage: /notepad_write <content>")
            return

        thread_id = make_thread_id(update.effective_chat.id)

        # Check for replace: prefix
        if raw.lower().startswith("replace:"):
            write_mode = "replace"
            content = raw[8:].strip()
        else:
            write_mode = "append"
            content = raw

        try:
            from ..tools.thread_notes import read_notepad, _notepad_path, MAX_NOTEPAD_SIZE
            path = _notepad_path(thread_id)

            if write_mode == "append":
                existing = path.read_text(encoding="utf-8") if path.exists() else ""
                if existing:
                    new_content = existing.rstrip() + "\n\n" + content
                else:
                    new_content = content
            else:
                new_content = content

            if len(new_content.encode("utf-8")) > MAX_NOTEPAD_SIZE:
                await update.message.reply_text(
                    f"Notepad would exceed {MAX_NOTEPAD_SIZE // 1024}KB limit."
                )
                return

            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new_content, encoding="utf-8")
            size = len(new_content.encode("utf-8"))
            await update.message.reply_text(f"Notepad updated ({write_mode}): {size} bytes.")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_notepad_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /notepad_clear."""
        thread_id = make_thread_id(update.effective_chat.id)
        try:
            from ..tools.thread_notes import delete_notepad
            if delete_notepad(thread_id):
                await update.message.reply_text("Notepad cleared.")
            else:
                await update.message.reply_text("Notepad was already empty.")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    # =========================================================================
    # Callback Query Handlers
    # =========================================================================

    async def _on_stop_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle stop button press during streaming."""
        query = update.callback_query
        await query.answer("Abort signal sent.")
        thread_id = query.data.split(":", 1)[1]
        try:
            await self.api.stop(thread_id)
        except Exception as e:
            logger.warning(f"Failed to stop thread via button: {e}")

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
        thread_id = make_thread_id(chat_id)

        nymeria_user_id = await self.resolve_user_id(telegram_user_id)
        if nymeria_user_id is None:
            try:
                await update.message.reply_text(
                    "This Telegram account isn't linked to a Nymeria user yet.\n"
                    "Ask the admin to run: "
                    f"`python run.py users link-platform <email> telegram {telegram_user_id}`"
                )
            except Exception:
                pass
            return

        text = (update.message.text or update.message.caption or "").strip()
        attachments, errors = await self._collect_attachments(update, context)

        for err in errors:
            try:
                await context.bot.send_message(chat_id=chat_id, text=err)
            except Exception:
                pass

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
                    errors.append("Couldn't download that photo — try resending.")

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
                    errors.append("Couldn't download that file — try resending.")

        if len(attachments) > attachment_helpers.MAX_FILES_PER_MESSAGE:
            extra = len(attachments) - attachment_helpers.MAX_FILES_PER_MESSAGE
            attachments = attachments[: attachment_helpers.MAX_FILES_PER_MESSAGE]
            errors.append(
                f"Skipped {extra} extra file(s) — max "
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

                            await self._handle_sse_event(event)

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

    async def _handle_sse_event(self, event: Dict[str, Any]) -> None:
        """Stream an autonomous-task event to the matching Telegram chat.

        Mirrors the per-event splitting that ``_stream_to_chat`` does for
        regular chat: response chunks accumulate in a per-thread buffer and
        get flushed at every tool_call boundary, so preamble text, tool
        announcements, and post-tool replies each land in their own bubble.
        No wrapper header — bubbles look identical to regular chat.
        """
        event_type = event.get("type", "")
        thread_id = event.get("thread_id", "")

        if not thread_id.startswith("telegram_"):
            return

        try:
            chat_id = int(thread_id[len("telegram_"):])
        except ValueError:
            return

        state = self._autonomous_state.get(thread_id)

        def _ensure_state() -> Dict[str, Any]:
            nonlocal state
            if state is None:
                state = {"chat_id": chat_id, "buffer": "", "tool_count": 0}
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

            elif event_type == "response":
                s = _ensure_state()
                chunk = event.get("content", "")
                if not chunk:
                    return
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

            elif event_type == "workspace_artifact":
                attach_path = event.get("path")
                if isinstance(attach_path, str) and attach_path:
                    await self._send_file_attachment(chat_id, attach_path)

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
                    if not s["buffer"] and not s["tool_count"]:
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
