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
                "name": "tools core",
                "path": ["tools", "core"],
                "usage": "/tools core",
                "description": "Show core tools",
                "category": "Tools",
                "aliases": ["/tools_core"],
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
    callback_query: _FakeCallbackQuery | None = None,
):
    bot = _FakeBot()
    message = None if callback_query else _FakeMessage(bot, text=text, chat_id=chat_id)
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=telegram_user_id),
        effective_chat=SimpleNamespace(id=chat_id, type="private"),
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
    assert "/tools_core: Show core tools" in text
    assert "/todo_add: Add a TODO" in text
    assert "/tools_search: Search tools" in text
    assert "/env_get:" not in text
    assert text.count("/compact:") == 1


def test_telegram_env_get_is_not_registered_as_admin_command():
    assert "env_get" not in TELEGRAM_COMMAND_ACCESS


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

    assert api.command_calls == [
        {
            "command": "/tools core",
            "thread_id": "telegram_123",
            "source": "user",
            "actor": "user",
            "surface": "telegram",
            "user_id": "user-1",
        }
    ]
    assert [msg.text for msg in fake_bot.messages] == [
        "backend result for /tools core"
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
            "attachments": None,
            "force_unsupported_attachments": False,
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
