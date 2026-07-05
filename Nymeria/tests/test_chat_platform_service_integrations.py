import json


from _service_integration_helpers import (  # type: ignore[import-not-found]
    bind_vault_repo as _use_repo,
    make_vault_repo as _repo,
)


def test_discord_send_channel_message_uses_env_bot_token(monkeypatch):
    from nymeria.tools import chat_platform_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "discord-token")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"id": "message-1", "content": "hello"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.discord_send_channel_message.func(
            channel_id="channel-1",
            content="hello",
            embeds_json='[{"title": "Example"}]',
        )
    )

    assert result["id"] == "message-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://discord.com/api/v10/channels/channel-1/messages"
    assert captured["headers"]["Authorization"] == "Bot discord-token"
    assert captured["json_body"] == {"content": "hello", "embeds": [{"title": "Example"}]}


def test_discord_send_channel_message_uses_vault_token(tmp_path, monkeypatch):
    from nymeria.tools import chat_platform_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Discord",
        provider="discord",
        kind="api_key",
        allowed_targets=["native_tool:discord_send_channel_message"],
        secret_fields={"accessToken": "discord-vault-token"},
        created_by_user_id="alice",
    )
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    captured = {}

    def fake_request(method, url, params=None, json_body=None, data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"id": "message-2", "content": "hello"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.discord_send_channel_message.func(
            channel_id="channel-1",
            content="hello",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["id"] == "message-2"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://discord.com/api/v10/channels/channel-1/messages"
    assert captured["headers"]["Authorization"] == "Bot discord-vault-token"


def test_telegram_send_message_uses_env_bot_token(monkeypatch):
    from nymeria.tools import chat_platform_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:telegram-token")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body})
        return {"ok": True, "result": {"message_id": 12}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.telegram_send_message.func(
            chat_id="chat-1",
            text="hello",
            parse_mode="Markdown",
            reply_markup_json='{"remove_keyboard": true}',
        )
    )

    assert result["ok"] is True
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.telegram.org/bot123:telegram-token/sendMessage"
    assert captured["json_body"]["chat_id"] == "chat-1"
    assert captured["json_body"]["parse_mode"] == "Markdown"
    assert captured["json_body"]["reply_markup"] == {"remove_keyboard": True}


def test_whatsapp_send_text_message_uses_env_token(monkeypatch):
    from nymeria.tools import chat_platform_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "wa-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "phone-1")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"messages": [{"id": "wamid.1"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.whatsapp_send_text_message.func(
            to="15551234567",
            text="hello",
            preview_url=True,
        )
    )

    assert result["messages"][0]["id"] == "wamid.1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://graph.facebook.com/v19.0/phone-1/messages"
    assert captured["headers"]["Authorization"] == "Bearer wa-token"
    assert captured["json_body"]["messaging_product"] == "whatsapp"
    assert captured["json_body"]["text"] == {"body": "hello", "preview_url": True}


def test_chat_platform_missing_credentials_return_setup_hints(monkeypatch):
    from nymeria.tools import chat_platform_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setattr(tools, "_settings_value", lambda name: None)
    for name in [
        "DISCORD_BOT_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "WHATSAPP_ACCESS_TOKEN",
    ]:
        monkeypatch.delenv(name, raising=False)

    telegram_result = tools.telegram_get_me.func()
    whatsapp_result = tools.whatsapp_list_phone_numbers.func()
    discord_result = tools.discord_get_channel.func(channel_id="channel-1")

    assert 'provider "telegram"' in telegram_result
    assert "TELEGRAM_BOT_TOKEN" in telegram_result
    assert 'provider "whatsapp"' in whatsapp_result
    assert "WHATSAPP_ACCESS_TOKEN" in whatsapp_result
    assert 'provider "discord"' in discord_result
    assert "DISCORD_BOT_TOKEN" in discord_result


def test_chat_platform_tools_are_registered_with_metadata():
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = [
        "telegram_get_me",
        "telegram_get_chat",
        "whatsapp_list_phone_numbers",
        "whatsapp_get_media_url",
        "discord_list_guild_channels",
        "discord_get_channel",
        "discord_get_channel_messages",
    ]
    moderate_names = [
        "telegram_send_message",
        "telegram_delete_message",
        "whatsapp_send_text_message",
        "whatsapp_send_template_message",
        "whatsapp_delete_media",
        "discord_send_channel_message",
        "discord_delete_message",
    ]

    for name in safe_names:
        assert name in CATALOG_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.INTEGRATIONS
        assert metadata.security_level == SecurityLevel.SAFE

    for name in moderate_names:
        assert name in CATALOG_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.INTEGRATIONS
        assert metadata.security_level == SecurityLevel.MODERATE


def test_removed_platform_tools_are_gone():
    # The 2026-07-05 bot cull removed the webex/mattermost/matrix/rocketchat/
    # zulip integration tools alongside their bots.
    import nymeria.tools as tools_pkg
    from nymeria.tools import CATALOG_TOOLS

    for name in (
        "webex_send_message",
        "mattermost_create_post",
        "matrix_send_room_message",
        "rocketchat_post_message",
        "zulip_send_message",
    ):
        assert name not in CATALOG_TOOLS
        assert not hasattr(tools_pkg, name)


def test_chat_platform_tool_schemas_hide_runtime_config():
    from nymeria.tools import (
        discord_get_channel_messages,
        telegram_send_message,
        whatsapp_send_template_message,
    )

    assert "config" not in telegram_send_message.args_schema.model_json_schema()["properties"]
    assert "config" not in whatsapp_send_template_message.args_schema.model_json_schema()["properties"]
    assert "config" not in discord_get_channel_messages.args_schema.model_json_schema()["properties"]
