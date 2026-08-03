"""Regression tests for Telegram streaming delivery."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from telegram.error import BadRequest

from nymeria.triggers.telegram_bot import (
    NymeriaTelegramBot,
    TELEGRAM_COMMAND_ACCESS,
    TELEGRAM_TEXT_LIMIT,
    _StopButtonToken,
    _find_thread_match,
    _sorted_switchable_threads,
)


class _FakeMessage:
    def __init__(
        self,
        bot: "_FakeBot",
        text: str = "",
        reply_markup=None,
        chat_id: int = 123,
    ):
        self.bot = bot
        self.text = text
        self.caption = None
        self.photo = []
        self.document = None
        self.voice = None
        self.audio = None
        self.reply_markup = reply_markup
        self.chat = SimpleNamespace(id=chat_id, type="private")
        self.replies: list[str] = []
        self.reply_to_message = None

    async def edit_text(self, text: str, **kwargs) -> None:
        self.bot._reject_if_too_long(text)
        self.text = text
        self.bot.edits.append(text)

    async def edit_reply_markup(self, reply_markup=None) -> None:
        self.reply_markup = reply_markup
        self.bot.reply_markup_edits += 1

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append(text)
        self.bot.replies.append(text)


class _FakeBot:
    def __init__(self):
        self.messages: list[_FakeMessage] = []
        self.edits: list[str] = []
        self.replies: list[str] = []
        self.reply_markup_edits = 0
        self.id = 999

    def _reject_if_too_long(self, text: str) -> None:
        if len(text) > TELEGRAM_TEXT_LIMIT:
            raise BadRequest("Message is too long")

    async def send_message(self, chat_id: int, text: str, **kwargs) -> _FakeMessage:
        self._reject_if_too_long(text)
        msg = _FakeMessage(
            self,
            text,
            reply_markup=kwargs.get("reply_markup"),
            chat_id=chat_id,
        )
        self.messages.append(msg)
        return msg

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        return None


class _FakeAPI:
    def __init__(self, text: str):
        self.text = text

    async def chat_stream(self, *args, **kwargs):
        yield {"type": "response", "content": self.text}
        yield {"type": "done"}


class _FakeEventAPI:
    def __init__(self, events: list[dict]):
        self.events = events

    async def chat_stream(self, *args, **kwargs):
        for event in self.events:
            yield event


class _CaptureAPI:
    def __init__(
        self,
        *,
        user_map: dict[str, str | None] | None = None,
        role: str = "user",
        events: list[dict] | None = None,
    ):
        self.user_map = user_map or {"42": "user-1"}
        self.role = role
        self.events = events or [{"type": "done"}]
        self.chat_calls: list[dict] = []
        self.stop_calls: list[tuple[str, str | None]] = []
        self.context_calls: list[tuple[str, str | None]] = []
        self.thread_config_calls: list[tuple[str, str | None]] = []
        self.history_calls: list[tuple[str, str | None]] = []
        self.command_calls: list[dict] = []
        self.list_command_calls: list[dict] = []

    async def resolve_platform_user(self, platform: str, platform_user_id: str):
        assert platform == "telegram"
        return self.user_map.get(platform_user_id)

    async def get_me(self, act_as: str | None = None):
        return {"id": act_as or "user-1", "role": self.role}

    async def stop(self, thread_id: str, user_id: str | None = None):
        self.stop_calls.append((thread_id, user_id))
        return {"status": "stopping"}

    async def get_context_stats(self, thread_id: str, user_id: str | None = None):
        self.context_calls.append((thread_id, user_id))
        return {
            "usage_percentage": 0,
            "total_tokens": 0,
            "context_limit": 1000,
            "compaction_count": 0,
            "context_management": "auto_compact",
        }

    async def get_thread_config(self, thread_id: str, user_id: str | None = None):
        self.thread_config_calls.append((thread_id, user_id))
        return {}

    async def get_history(self, thread_id: str, user_id: str | None = None):
        self.history_calls.append((thread_id, user_id))
        return {"messages": []}

    async def chat_stream(self, message: str, thread_id: str, user_id: str, **kwargs):
        self.chat_calls.append(
            {
                "message": message,
                "thread_id": thread_id,
                "user_id": user_id,
                **kwargs,
            }
        )
        for event in self.events:
            yield event

    async def chat(self, message: str, thread_id: str, user_id: str, **kwargs):
        self.chat_calls.append(
            {
                "message": message,
                "thread_id": thread_id,
                "user_id": user_id,
                **kwargs,
            }
        )
        return {"response": "", "tool_call_count": 0}

    async def execute_command(self, command: str, **kwargs):
        self.command_calls.append({"command": command, **kwargs})
        return {
            "success": True,
            "markdown": f"backend result for {command}",
            "command": command.lstrip("/"),
            "level": "success",
            "data": None,
        }

    async def list_commands(self, **kwargs):
        self.list_command_calls.append(kwargs)
        return [
            {
                "name": "compact",
                "path": ["compact"],
                "usage": "/compact",
                "description": "Compact the active chat context",
                "category": "Thread",
                "execution_kind": "chat_stream",
            },
            {
                "name": "tools list",
                "path": ["tools", "list"],
                "usage": "/tools list [enabled|optional|core|<category>]",
                "description": "List tools",
                "category": "Tools",
                # The menu name is the FIRST single-token alias, so this row
                # also pins that the retired `/tools_core` spelling no longer
                # names the folded listing (backlog #131).
                "aliases": ["/tools_list", "/tools_core", "/tools_enabled"],
                "execution_kind": "command",
            },
            {
                "name": "todos add",
                "path": ["todos", "add"],
                "usage": "/todos add",
                "description": "Add a TODO",
                "category": "TODOs",
                "aliases": ["/todos_add"],
                "execution_kind": "command",
            },
        ]


class _FakeCallbackQuery:
    def __init__(self, *, data: str, chat_id: int = 123):
        self.data = data
        self.message = _FakeMessage(_FakeBot(), chat_id=chat_id)
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str, show_alert: bool = False):
        self.answers.append((text, show_alert))


def _fake_update(
    *,
    telegram_user_id: int = 42,
    chat_id: int = 123,
    text: str = "hello",
    chat_type: str = "private",
    callback_query: _FakeCallbackQuery | None = None,
):
    bot = _FakeBot()
    message = None if callback_query else _FakeMessage(bot, text=text, chat_id=chat_id)
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=telegram_user_id),
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
        message=message,
        callback_query=callback_query,
    )


async def _deliver_autonomous(bot: NymeriaTelegramBot, events: list[dict]) -> None:
    for event in events:
        await bot._handle_sse_event(event)


def test_telegram_thread_match_prefers_titles_and_skips_native_threads():
    rows = [
        {"thread_id": "telegram_123", "title": "Default Telegram"},
        {"thread_id": "desktop-a", "title": "Project Alpha", "updated_at": "2026-05-01"},
        {"thread_id": "desktop-b", "title": "Project Beta", "updated_at": "2026-05-02"},
    ]

    switchable = _sorted_switchable_threads(rows)
    assert [row["thread_id"] for row in switchable] == ["desktop-b", "desktop-a"]

    match, ambiguous, reason = _find_thread_match(rows, "Project Alpha")
    assert reason == "exact_title"
    assert ambiguous == []
    assert match["thread_id"] == "desktop-a"


def test_telegram_thread_match_reports_ambiguous_title_substrings():
    rows = [
        {"thread_id": "desktop-a", "title": "Project Alpha"},
        {"thread_id": "desktop-b", "title": "Project Beta"},
    ]

    match, ambiguous, reason = _find_thread_match(rows, "project")

    assert match is None
    assert reason == "ambiguous"
    assert {row["thread_id"] for row in ambiguous} == {"desktop-a", "desktop-b"}


def test_telegram_thread_match_uses_cached_numbers():
    rows = [
        {"thread_id": "desktop-a", "title": "Project Alpha"},
        {"thread_id": "desktop-b", "title": "Project Beta"},
    ]
    cached = [rows[1], rows[0]]

    match, ambiguous, reason = _find_thread_match(rows, "2", cached)

    assert reason == "number"
    assert ambiguous == []
    assert match["thread_id"] == "desktop-a"


def test_telegram_stream_splits_single_oversized_response_chunk():
    long_response = "A" * (TELEGRAM_TEXT_LIMIT * 2 + 211)
    fake_bot = _FakeBot()
    bot = NymeriaTelegramBot(
        api=_FakeAPI(long_response),
        bot_token="test-token",
    )
    context = SimpleNamespace(bot=fake_bot)

    asyncio.run(
        bot._stream_to_chat(
            chat_id=123,
            message="prompt",
            thread_id="telegram_123",
            user_id="user-1",
            context=context,
        )
    )

    sent_text = "".join(msg.text for msg in fake_bot.messages)
    assert sent_text == long_response
    assert len(fake_bot.messages) >= 3
    assert all(len(msg.text) <= TELEGRAM_TEXT_LIMIT for msg in fake_bot.messages)
    assert fake_bot.messages[0].reply_markup is None
    assert fake_bot.reply_markup_edits == 1


def test_telegram_stream_surfaces_compaction_events():
    fake_bot = _FakeBot()
    bot = NymeriaTelegramBot(
        api=_FakeEventAPI([
            {"type": "compacting", "message": "Compacting context..."},
            {
                "type": "compacted",
                "messages_removed": 7,
                "summary": "Prior task state and decisions.",
            },
            {"type": "response", "content": "Continuing now."},
            {"type": "done"},
        ]),
        bot_token="test-token",
    )
    context = SimpleNamespace(bot=fake_bot)

    asyncio.run(
        bot._stream_to_chat(
            chat_id=123,
            message="prompt",
            thread_id="telegram_123",
            user_id="user-1",
            context=context,
        )
    )

    texts = [msg.text for msg in fake_bot.messages]
    assert any("Compacting context" in text for text in texts)
    assert any("Context compacted" in text and "Prior task state" in text for text in texts)
    assert any("Continuing now." in text for text in texts)


def test_telegram_autonomous_does_not_duplicate_flushed_response_on_completion():
    long_response = "A" * (TELEGRAM_TEXT_LIMIT + 211)
    fake_bot = _FakeBot()
    bot = NymeriaTelegramBot(
        api=_FakeEventAPI([]),
        bot_token="test-token",
    )
    bot._application = SimpleNamespace(bot=fake_bot)

    asyncio.run(_deliver_autonomous(bot, [
        {"type": "task_started", "thread_id": "telegram_123"},
        {"type": "response", "thread_id": "telegram_123", "content": long_response},
        {
            "type": "task_completed",
            "thread_id": "telegram_123",
            "content": long_response,
        },
    ]))

    sent_text = "".join(msg.text for msg in fake_bot.messages)
    assert sent_text == long_response
    assert len(fake_bot.messages) >= 2
    assert bot._autonomous_state == {}


def test_telegram_autonomous_surfaces_compaction_and_iteration_events():
    fake_bot = _FakeBot()
    bot = NymeriaTelegramBot(
        api=_FakeEventAPI([]),
        bot_token="test-token",
    )
    bot._application = SimpleNamespace(bot=fake_bot)

    asyncio.run(_deliver_autonomous(bot, [
        {"type": "task_started", "thread_id": "telegram_123"},
        {
            "type": "compacted",
            "thread_id": "telegram_123",
            "messages_removed": 7,
            "summary": "Prior task state.",
        },
        {
            "type": "context_attached",
            "thread_id": "telegram_123",
            "summary": "Attached summary.",
        },
        {
            "type": "iteration_limit",
            "thread_id": "telegram_123",
            "content": "Stopped after too many tool calls.",
        },
        {"type": "task_completed", "thread_id": "telegram_123", "content": ""},
    ]))

    texts = [msg.text for msg in fake_bot.messages]
    assert any("Context compacted" in text and "Prior task state" in text for text in texts)
    assert any("Context summary attached" in text and "Attached summary" in text for text in texts)
    assert any("Stopped after too many tool calls" in text for text in texts)


def test_telegram_command_policy_rejects_unlinked_linked_command():
    api = _CaptureAPI(user_map={"42": None})
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    called = False

    async def handler(update, context):
        nonlocal called
        called = True

    update = _fake_update(telegram_user_id=42)
    context = SimpleNamespace(bot=_FakeBot(), args=[])

    asyncio.run(bot._guarded_command("thread", handler)(update, context))

    assert called is False
    assert "isn't linked" in update.message.replies[0]


def test_telegram_command_policy_renders_infra_copy_on_resolver_failure():
    # Backlog #108: a backend auth failure (expired service token) must render
    # infrastructure copy, never account-link instructions.
    import httpx

    class _AuthDownAPI(_CaptureAPI):
        async def resolve_platform_user(self, platform: str, platform_user_id: str):
            request = httpx.Request("GET", "http://api.test/platform/resolve")
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError("401", request=request, response=response)

    api = _AuthDownAPI()
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    called = False

    async def handler(update, context):
        nonlocal called
        called = True

    update = _fake_update(telegram_user_id=42)
    context = SimpleNamespace(bot=_FakeBot(), args=[])

    asyncio.run(bot._guarded_command("thread", handler)(update, context))

    assert called is False
    reply = update.message.replies[0]
    assert "isn't linked" not in reply
    assert "service token" in reply


def test_telegram_command_policy_rejects_non_admin_admin_command():
    api = _CaptureAPI(user_map={"42": "user-1"}, role="user")
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    called = False

    async def handler(update, context):
        nonlocal called
        called = True

    update = _fake_update(telegram_user_id=42)
    context = SimpleNamespace(bot=_FakeBot(), args=[])

    asyncio.run(bot._guarded_command("restart", handler)(update, context))

    assert called is False
    assert update.message.replies == ["Admin only."]


def test_telegram_public_command_policy_does_not_require_linked_user():
    api = _CaptureAPI(user_map={"42": None})
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    called = False

    async def handler(update, context):
        nonlocal called
        called = True

    update = _fake_update(telegram_user_id=42)
    context = SimpleNamespace(bot=_FakeBot(), args=[])

    asyncio.run(bot._guarded_command("help", handler)(update, context))

    assert called is True
    assert update.message.replies == []


def test_telegram_help_merges_backend_catalog_with_local_commands():
    api = _CaptureAPI(user_map={"42": "user-1"})
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    update = _fake_update(telegram_user_id=42)
    fake_bot = _FakeBot()
    context = SimpleNamespace(bot=fake_bot, args=[])

    asyncio.run(bot._cmd_help(update, context))

    assert api.list_command_calls == [
        {"actor": "user", "surface": "telegram", "user_id": "user-1"}
    ]
    text = fake_bot.messages[0].text
    assert "/tools_list: List tools" in text
    assert "/tools_core:" not in text
    assert "/todo_add: Add a TODO" in text
    assert "/tools_search: Search tools" in text
    assert "/env_get:" not in text
    assert text.count("/compact:") == 1


def test_telegram_env_get_is_not_registered_as_admin_command():
    assert "env_get" not in TELEGRAM_COMMAND_ACCESS


def _registered_telegram_command_names() -> set[str]:
    from telegram.ext import CommandHandler

    class _Recorder:
        def __init__(self):
            self.handlers = []

        def add_handler(self, handler):
            self.handlers.append(handler)

        def add_error_handler(self, handler):
            pass

    bot = NymeriaTelegramBot(api=_CaptureAPI(), bot_token="test-token")
    app = _Recorder()
    bot._register_handlers(app)
    return {
        name
        for handler in app.handlers
        if isinstance(handler, CommandHandler)
        for name in handler.commands
    }


def test_every_access_policy_row_names_a_registered_command():
    """A policy keyed on a name no handler claims is a DEAD gate.

    `_check_command_access` runs only from `_guarded_command`, so a key that
    matches no `CommandHandler` gates nothing: that spelling falls through to
    the catch-all and the backend's own `requires_admin`. Backlog #131 renamed
    `/config_show` to `/settings`; leaving the old keys here would have
    silently dropped the Telegram pre-gate on the settings family.
    """
    registered = _registered_telegram_command_names()
    orphaned = sorted(set(TELEGRAM_COMMAND_ACCESS) - registered)
    assert not orphaned, f"access policy names unregistered commands: {orphaned}"


def test_the_renamed_settings_commands_keep_the_admin_gate():
    """The #131 rename moved the keys; it must not have widened the gate."""
    api = _CaptureAPI(user_map={"42": "user-1"}, role="user")
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")

    for name in ("settings", "settings_get", "settings_set"):
        called = False

        async def handler(update, context):
            nonlocal called
            called = True

        update = _fake_update(telegram_user_id=42)
        context = SimpleNamespace(bot=_FakeBot(), args=[])

        asyncio.run(bot._guarded_command(name, handler)(update, context))

        assert called is False, f"/{name} ran for a non-admin"
        assert update.message.replies == ["Admin only."]
    assert api.command_calls == []


def test_telegram_thread_command_uses_backend_command_service():
    api = _CaptureAPI(user_map={"42": "user-1"})
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    update = _fake_update(telegram_user_id=42)
    fake_bot = _FakeBot()
    context = SimpleNamespace(bot=fake_bot, args=[])

    asyncio.run(bot._cmd_thread(update, context))

    assert api.command_calls == [
        {
            "command": "/thread",
            "thread_id": "telegram_123",
            "source": "user",
            "actor": "user",
            "surface": "telegram",
            "user_id": "user-1",
        }
    ]
    assert [msg.text for msg in fake_bot.messages] == ["backend result for /thread"]


def test_telegram_compact_uses_chat_stream_endpoint():
    api = _CaptureAPI(
        user_map={"42": "user-1"},
        events=[
            {"type": "compacting", "message": "Compacting thread context..."},
            {"type": "response", "content": "Compacted."},
            {"type": "done"},
        ],
    )
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    update = _fake_update(telegram_user_id=42)
    fake_bot = _FakeBot()
    context = SimpleNamespace(bot=fake_bot, args=[])

    asyncio.run(bot._cmd_compact(update, context))

    assert api.command_calls == []
    assert api.chat_calls[0]["message"] == "/compact"
    assert api.chat_calls[0]["thread_id"] == "telegram_123"
    assert api.chat_calls[0]["user_id"] == "user-1"
    texts = [msg.text for msg in fake_bot.messages]
    assert any("Compacting thread context" in text for text in texts)


def test_telegram_global_tool_command_uses_backend_command_service():
    api = _CaptureAPI(user_map={"42": "user-1"})
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    update = _fake_update(telegram_user_id=42)
    fake_bot = _FakeBot()
    context = SimpleNamespace(bot=fake_bot, args=[])

    asyncio.run(bot._cmd_tools_core(update, context))

    # The relay carries the FILTER VALUE, not the retired `tools core` alias:
    # an alias substitutes a path and cannot inject a value, so relaying the
    # old spelling would render the enabled view instead of the core one.
    assert api.command_calls == [
        {
            "command": "/tools list core",
            "thread_id": "telegram_123",
            "source": "user",
            "actor": "user",
            "surface": "telegram",
            "user_id": "user-1",
        }
    ]
    assert [msg.text for msg in fake_bot.messages] == [
        "backend result for /tools list core"
    ]


def test_telegram_global_memory_command_passes_arguments_to_backend_command_service():
    api = _CaptureAPI(user_map={"42": "user-1"})
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    update = _fake_update(telegram_user_id=42)
    fake_bot = _FakeBot()
    context = SimpleNamespace(bot=fake_bot, args=["search", "term"])

    asyncio.run(bot._cmd_memory_search(update, context))

    assert api.command_calls[0]["command"] == "/memory search search term"
    assert api.command_calls[0]["surface"] == "telegram"
    assert api.command_calls[0]["user_id"] == "user-1"


def test_telegram_global_todo_command_passes_arguments_to_backend_command_service():
    api = _CaptureAPI(user_map={"42": "user-1"})
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    update = _fake_update(telegram_user_id=42)
    fake_bot = _FakeBot()
    context = SimpleNamespace(bot=fake_bot, args=["Check", "logs", "|", "2h"])

    asyncio.run(bot._cmd_todo_add(update, context))

    assert api.command_calls[0]["command"] == "/todos add Check logs | 2h"
    assert api.command_calls[0]["thread_id"] == "telegram_123"
    assert api.command_calls[0]["surface"] == "telegram"
    assert api.command_calls[0]["user_id"] == "user-1"


def test_telegram_thread_tool_mutation_uses_backend_command_service():
    api = _CaptureAPI(user_map={"42": "user-1"})
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    update = _fake_update(telegram_user_id=42)
    fake_bot = _FakeBot()
    context = SimpleNamespace(bot=fake_bot, args=["browser"])

    asyncio.run(bot._cmd_tools_enable(update, context))

    assert api.command_calls[0]["command"] == "/tools enable browser"
    assert api.command_calls[0]["thread_id"] == "telegram_123"
    assert api.command_calls[0]["surface"] == "telegram"
    assert api.command_calls[0]["user_id"] == "user-1"


def test_telegram_notepad_command_uses_backend_command_service():
    api = _CaptureAPI(user_map={"42": "user-1"})
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    update = _fake_update(telegram_user_id=42)
    fake_bot = _FakeBot()
    context = SimpleNamespace(bot=fake_bot, args=["replace:project", "notes"])

    asyncio.run(bot._cmd_notepad_write(update, context))

    assert api.command_calls[0]["command"] == "/notepad write replace:project notes"
    assert api.command_calls[0]["thread_id"] == "telegram_123"
    assert api.command_calls[0]["surface"] == "telegram"
    assert api.command_calls[0]["user_id"] == "user-1"


def test_telegram_export_command_reads_history_as_linked_user():
    api = _CaptureAPI(user_map={"42": "user-1"})
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    update = _fake_update(telegram_user_id=42)
    context = SimpleNamespace(bot=_FakeBot(), args=[])

    asyncio.run(bot._cmd_export(update, context))

    assert api.history_calls == [("telegram_123", "user-1")]
    assert update.message.replies == ["No conversation history to export."]


def test_telegram_plain_message_preserves_thread_mention_for_backend_dispatch():
    api = _CaptureAPI(user_map={"42": "user-1"})
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    fake_bot = _FakeBot()
    update = _fake_update(text='@"Research Notes" summarize this', telegram_user_id=42)
    context = SimpleNamespace(bot=fake_bot)

    asyncio.run(bot._on_message(update, context))

    assert api.chat_calls == [
        {
            "message": '@"Research Notes" summarize this',
            "thread_id": "telegram_123",
            "user_id": "user-1",
            "is_self_invoke": False,
            "trigger_override": None,
            "attachments": None,
            "force_unsupported_attachments": False,
            "source": None,
            "source_label": None,
            "publish_autonomous_events": None,
            "platform_origin": None,
        }
    ]


def test_telegram_ask_command_preserves_thread_mention_for_backend_dispatch():
    api = _CaptureAPI(user_map={"42": "user-1"})
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    update = _fake_update(telegram_user_id=42)
    context = SimpleNamespace(bot=_FakeBot(), args=["@Research", "compare", "notes"])

    asyncio.run(bot._cmd_ask(update, context))

    assert api.chat_calls[0]["message"] == "@Research compare notes"
    assert api.chat_calls[0]["thread_id"] == "telegram_123"
    assert api.chat_calls[0]["user_id"] == "user-1"


def test_telegram_stream_renders_dispatched_thread_reference():
    fake_bot = _FakeBot()
    bot = NymeriaTelegramBot(
        api=_FakeEventAPI([
            {
                "type": "dispatched",
                "thread_id": "telegram_123",
                "target_thread_id": "thread-research",
                "title": "Research",
                "dispatched_to": {
                    "thread_id": "thread-research",
                    "title": "Research",
                    "original_thread_id": "telegram_123",
                },
            },
            {"type": "response", "content": "Done."},
            {"type": "done"},
        ]),
        bot_token="test-token",
    )
    context = SimpleNamespace(bot=fake_bot)

    asyncio.run(
        bot._stream_to_chat(
            chat_id=123,
            message="@Research compare notes",
            thread_id="telegram_123",
            user_id="user-1",
            context=context,
            telegram_user_id=42,
        )
    )

    sent_text = "".join(msg.text for msg in fake_bot.messages)
    assert "Response from Research" in sent_text
    assert "Done." in sent_text


def test_telegram_stream_renders_ambiguous_mention_error():
    fake_bot = _FakeBot()
    bot = NymeriaTelegramBot(
        api=_FakeEventAPI([
            {
                "type": "error",
                "code": "mention_ambiguous",
                "content": "@Research matches multiple threads: Research Alpha, Research Beta",
            },
            {"type": "done", "status": "error"},
        ]),
        bot_token="test-token",
    )
    context = SimpleNamespace(bot=fake_bot)

    asyncio.run(
        bot._stream_to_chat(
            chat_id=123,
            message="@Research compare notes",
            thread_id="telegram_123",
            user_id="user-1",
            context=context,
            telegram_user_id=42,
        )
    )

    assert any(
        "Research matches multiple threads" in message.text
        for message in fake_bot.messages
    )


def test_telegram_stop_button_valid_token_stops_as_requester():
    api = _CaptureAPI(user_map={"42": "user-1"})
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    token = bot._create_stop_button_token(
        chat_id=123,
        thread_id="telegram_123",
        nymeria_user_id="user-1",
        telegram_user_id=42,
    )
    query = _FakeCallbackQuery(data=f"stop:{token}", chat_id=123)
    update = _fake_update(telegram_user_id=42, callback_query=query)
    context = SimpleNamespace(bot=_FakeBot())

    asyncio.run(bot._on_stop_button(update, context))

    assert api.stop_calls == [("telegram_123", "user-1")]
    assert query.answers == [("Abort signal sent.", False)]
    assert token not in bot._stop_button_tokens


def test_telegram_stop_button_rejects_wrong_user():
    api = _CaptureAPI(user_map={"43": "user-2"})
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    token = bot._create_stop_button_token(
        chat_id=123,
        thread_id="telegram_123",
        nymeria_user_id="user-1",
        telegram_user_id=42,
    )
    query = _FakeCallbackQuery(data=f"stop:{token}", chat_id=123)
    update = _fake_update(telegram_user_id=43, callback_query=query)
    context = SimpleNamespace(bot=_FakeBot())

    asyncio.run(bot._on_stop_button(update, context))

    assert api.stop_calls == []
    assert query.answers == [("Only the requester can stop this run.", True)]


def test_telegram_stop_button_rejects_wrong_chat():
    api = _CaptureAPI(user_map={"42": "user-1"})
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    token = bot._create_stop_button_token(
        chat_id=123,
        thread_id="telegram_123",
        nymeria_user_id="user-1",
        telegram_user_id=42,
    )
    query = _FakeCallbackQuery(data=f"stop:{token}", chat_id=999)
    update = _fake_update(telegram_user_id=42, chat_id=999, callback_query=query)
    context = SimpleNamespace(bot=_FakeBot())

    asyncio.run(bot._on_stop_button(update, context))

    assert api.stop_calls == []
    assert query.answers == [("That stop button belongs to another chat.", True)]


def test_telegram_stop_button_rejects_expired_token():
    api = _CaptureAPI(user_map={"42": "user-1"})
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    token = "expired"
    bot._stop_button_tokens[token] = _StopButtonToken(
        chat_id=123,
        thread_id="telegram_123",
        nymeria_user_id="user-1",
        telegram_user_id=42,
        expires_at=0,
    )
    query = _FakeCallbackQuery(data=f"stop:{token}", chat_id=123)
    update = _fake_update(telegram_user_id=42, callback_query=query)
    context = SimpleNamespace(bot=_FakeBot())

    asyncio.run(bot._on_stop_button(update, context))

    assert api.stop_calls == []
    assert query.answers == [("That stop button expired.", True)]


def test_telegram_autonomous_drops_fanout_mirror_events():
    """A queuer's mirror of the holder turn (stream_bridge fanout marker)
    must not render: without the gate, the marked stream interleaves into
    the same per-thread buffer and the marked task_completed re-delivers
    the content via the fallback (the 2026-07-19 interleaved-briefing bug).
    """
    fake_bot = _FakeBot()
    bot = NymeriaTelegramBot(
        api=_FakeEventAPI([]),
        bot_token="test-token",
    )
    bot._application = SimpleNamespace(bot=fake_bot)

    briefing = "Morning briefing: all quiet."
    asyncio.run(_deliver_autonomous(bot, [
        {"type": "task_started", "thread_id": "telegram_123", "task_id": "todo-1"},
        # The queuer task's mirror of the same turn, arriving interleaved.
        {
            "type": "response",
            "thread_id": "telegram_123",
            "task_id": "handoff-1",
            "content": briefing,
            "fanout": True,
        },
        # The holder's own stream.
        {
            "type": "response",
            "thread_id": "telegram_123",
            "task_id": "todo-1",
            "content": briefing,
        },
        # Mirror completion first (as observed live): must neither flush nor
        # pop the per-thread state.
        {
            "type": "task_completed",
            "thread_id": "telegram_123",
            "task_id": "handoff-1",
            "content": briefing,
            "fanout": True,
        },
        {
            "type": "task_completed",
            "thread_id": "telegram_123",
            "task_id": "todo-1",
            "content": briefing,
        },
    ]))

    sent_text = "".join(msg.text for msg in fake_bot.messages)
    assert sent_text == briefing
    assert bot._autonomous_state == {}


def test_telegram_command_registration_caps_at_telegram_limit():
    """Telegram rejects >100 registered commands (Bot_commands_too_much,
    fatal to post_init and crash-loops the container); the menu list must
    cap at the platform limit."""
    from nymeria.triggers.telegram_bot import (
        TELEGRAM_MAX_BOT_COMMANDS,
        _telegram_bot_commands_from_catalog,
    )

    catalog = [
        {"name": f"cmd{i:03d}", "description": f"Command {i}", "category": "Global"}
        for i in range(150)
    ]
    commands = _telegram_bot_commands_from_catalog(catalog)
    assert len(commands) == TELEGRAM_MAX_BOT_COMMANDS


# ── Catch-all backend command passthrough ───────────────────────────────────


def _command_context(bot=None, args=None):
    return SimpleNamespace(bot=bot or _FakeBot(), args=args or [])


def test_unregistered_command_forwards_to_backend():
    api = _CaptureAPI()
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    update = _fake_update(text="/provider list")
    context = _command_context()

    asyncio.run(bot._on_unregistered_command(update, context))

    assert api.command_calls, "command should reach the backend service"
    call = api.command_calls[0]
    assert call["command"] == "/provider list"
    assert call["surface"] == "telegram"
    sent = "".join(msg.text for msg in context.bot.messages)
    assert "backend result for /provider list" in sent


def test_unregistered_command_typo_relays_backend_copy():
    class _TypoAPI(_CaptureAPI):
        async def execute_command(self, command: str, **kwargs):
            self.command_calls.append({"command": command, **kwargs})
            return {
                "success": False,
                "markdown": "**Error:** Unknown command `/provder`. Did you mean `/provider`? Use `/help`.",
                "command": "provder",
                "level": "error",
                "data": None,
            }

    api = _TypoAPI()
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    update = _fake_update(text="/provder list")
    context = _command_context()

    asyncio.run(bot._on_unregistered_command(update, context))

    sent = "".join(msg.text for msg in context.bot.messages)
    assert "Did you mean `/provider`?" in sent


def test_unregistered_command_strips_own_bot_username_suffix():
    api = _CaptureAPI()
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    update = _fake_update(text="/provider@nymeria_bot list")
    fake_bot = _FakeBot()
    fake_bot.username = "nymeria_bot"
    context = _command_context(bot=fake_bot)

    asyncio.run(bot._on_unregistered_command(update, context))

    assert api.command_calls[0]["command"] == "/provider list"


def test_unregistered_command_ignores_other_bots_commands():
    api = _CaptureAPI()
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    update = _fake_update(text="/provider@other_bot list")
    fake_bot = _FakeBot()
    fake_bot.username = "nymeria_bot"
    context = _command_context(bot=fake_bot)

    asyncio.run(bot._on_unregistered_command(update, context))

    assert api.command_calls == []
    assert context.bot.messages == []


def test_unregistered_command_chat_stream_falls_through_to_chat():
    class _ChatStreamAPI(_CaptureAPI):
        async def execute_command(self, command: str, **kwargs):
            self.command_calls.append({"command": command, **kwargs})
            return {
                "success": False,
                "markdown": "**Error:** `/quick` is handled outside the command service.",
                "command": "quick",
                "level": "error",
                "data": {"execution_kind": "chat_stream"},
            }

    api = _ChatStreamAPI()
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    streamed: list[dict] = []

    async def fake_stream(**kwargs):
        streamed.append(kwargs)

    bot._stream_to_chat = fake_stream
    update = _fake_update(text="/quick what is 2+2")
    update.message.message_id = 555
    context = _command_context()

    asyncio.run(bot._on_unregistered_command(update, context))

    assert streamed and streamed[0]["message"] == "/quick what is 2+2"
    # The non-executable refusal must NOT be relayed to the user.
    assert context.bot.messages == []
    # The fall-through rides the normal chat path, so it must carry the same
    # platform_origin _on_message supplies (omitting it clears the origin
    # registry and kills the emoji-reaction path for these turns).
    origin = streamed[0]["platform_origin"]
    assert origin is not None
    assert origin["platform"] == "telegram"
    assert origin["message_id"] == "555"


def test_unregistered_command_ignored_in_groups_unless_addressed():
    api = _CaptureAPI()
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    update = _fake_update(text="/otherbotcmd", chat_type="group")
    context = _command_context()

    asyncio.run(bot._on_unregistered_command(update, context))

    # A bare unregistered command in a group likely belongs to another bot:
    # stay silent unless addressed (@suffix) or replied-to.
    assert api.command_calls == []
    assert context.bot.messages == []


def test_unregistered_command_in_group_answers_when_addressed():
    api = _CaptureAPI()
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    update = _fake_update(text="/provider@nymeria_bot list", chat_type="group")
    fake_bot = _FakeBot()
    fake_bot.username = "nymeria_bot"
    context = _command_context(bot=fake_bot)

    asyncio.run(bot._on_unregistered_command(update, context))

    assert api.command_calls and api.command_calls[0]["command"] == "/provider list"


def test_unregistered_command_in_group_answers_reply_to_bot():
    api = _CaptureAPI()
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    update = _fake_update(text="/provider list", chat_type="group")
    context = _command_context()
    update.message.reply_to_message = SimpleNamespace(
        from_user=SimpleNamespace(id=context.bot.id)
    )

    asyncio.run(bot._on_unregistered_command(update, context))

    assert api.command_calls and api.command_calls[0]["command"] == "/provider list"


def test_unregistered_command_unlinked_user_gets_link_copy():
    api = _CaptureAPI(user_map={"42": None})
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    update = _fake_update(text="/provider list")
    context = _command_context()

    asyncio.run(bot._on_unregistered_command(update, context))

    assert api.command_calls == []
    assert any("isn't linked" in reply for reply in update.message.replies)


def test_catch_all_command_handler_is_registered_last():
    from telegram.ext import CommandHandler, MessageHandler

    api = _CaptureAPI()
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")

    class _FakeApp:
        def __init__(self):
            self.handlers = []
            self.error_handlers = []

        def add_handler(self, handler):
            self.handlers.append(handler)

        def add_error_handler(self, handler):
            self.error_handlers.append(handler)

    app = _FakeApp()
    bot._register_handlers(app)

    message_handlers = [h for h in app.handlers if isinstance(h, MessageHandler)]
    assert message_handlers, "expected message handlers"
    catch_all = message_handlers[-1]
    assert catch_all.callback == bot._on_unregistered_command
    # The catch-all must come after every named CommandHandler so those keep
    # winning within the handler group.
    last_command_index = max(
        index
        for index, handler in enumerate(app.handlers)
        if isinstance(handler, CommandHandler)
    )
    assert app.handlers.index(catch_all) > last_command_index
