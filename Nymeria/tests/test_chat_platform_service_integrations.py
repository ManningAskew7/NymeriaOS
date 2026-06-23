import base64
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


def test_mattermost_create_post_uses_vault_token(tmp_path, monkeypatch):
    from nymeria.tools import chat_platform_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Mattermost",
        provider="mattermost",
        kind="api_key",
        allowed_targets=["native_tool:mattermost_create_post"],
        secret_fields={"accessToken": "mattermost-token", "baseUrl": "https://mattermost.example"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"id": "post-1", "message": "hello"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.mattermost_create_post.func(
            channel_id="channel-1",
            message="hello",
            props_json='{"attachments": [{"text": "extra"}]}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["id"] == "post-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://mattermost.example/api/v4/posts"
    assert captured["headers"]["Authorization"] == "Bearer mattermost-token"
    assert captured["json_body"]["props"]["attachments"][0]["text"] == "extra"


def test_matrix_send_room_message_uses_env_token(monkeypatch):
    from nymeria.tools import chat_platform_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "matrix-token")
    monkeypatch.setenv("MATRIX_BASE_URL", "https://matrix.example/_matrix/client/v3")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"event_id": "$event"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.matrix_send_room_message.func(
            room_id="!room:example",
            body="hello",
            formatted_body="<strong>hello</strong>",
            txn_id="txn-1",
        )
    )

    assert result["event_id"] == "$event"
    assert captured["method"] == "PUT"
    assert captured["url"] == "https://matrix.example/_matrix/client/v3/rooms/%21room%3Aexample/send/m.room.message/txn-1"
    assert captured["headers"]["Authorization"] == "Bearer matrix-token"
    assert captured["json_body"]["formatted_body"] == "<strong>hello</strong>"


def test_rocketchat_history_uses_vault_credentials(tmp_path, monkeypatch):
    from nymeria.tools import chat_platform_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Rocket.Chat",
        provider="rocketchat",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={
            "authKey": "rocket-token",
            "userId": "rocket-user",
            "domain": "https://rocket.example",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, data=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return {"messages": [{"_id": "message-1"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.rocketchat_get_channel_history.func(
            room_id="room-1",
            count=5,
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["messages"][0]["_id"] == "message-1"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://rocket.example/api/v1/channels.history"
    assert captured["headers"]["X-Auth-Token"] == "rocket-token"
    assert captured["headers"]["X-User-Id"] == "rocket-user"
    assert captured["params"]["roomId"] == "room-1"
    assert captured["params"]["count"] == 5


def test_zulip_send_message_uses_env_basic_auth(monkeypatch):
    from nymeria.tools import chat_platform_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("ZULIP_API_KEY", "zulip-key")
    monkeypatch.setenv("ZULIP_EMAIL", "bot@example.com")
    monkeypatch.setenv("ZULIP_BASE_URL", "https://zulip.example")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, data=None, headers=None):
        captured.update({"method": method, "url": url, "data": data, "headers": headers})
        return {"id": 42, "result": "success"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.zulip_send_message.func(
            message_type="stream",
            to="general",
            topic="updates",
            content="hello",
        )
    )

    assert result["id"] == 42
    expected = base64.b64encode(b"bot@example.com:zulip-key").decode()
    assert captured["method"] == "POST"
    assert captured["url"] == "https://zulip.example/api/v1/messages"
    assert captured["headers"]["Authorization"] == f"Basic {expected}"
    assert captured["data"] == {"type": "stream", "to": "general", "content": "hello", "topic": "updates"}


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


def test_webex_send_message_uses_vault_token(tmp_path, monkeypatch):
    from nymeria.tools import chat_platform_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Webex",
        provider="webex",
        kind="api_key",
        allowed_targets=["native_tool:webex_send_message"],
        secret_fields={"accessToken": "webex-token", "baseUrl": "https://webex.example/v1"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"id": "msg-1", "text": "hello"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.webex_send_message.func(
            room_id="room-1",
            markdown="**hello**",
            files="https://example.com/a.png, https://example.com/b.png",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["id"] == "msg-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://webex.example/v1/messages"
    assert captured["headers"]["Authorization"] == "Bearer webex-token"
    assert captured["json_body"]["roomId"] == "room-1"
    assert captured["json_body"]["markdown"] == "**hello**"
    assert captured["json_body"]["files"] == ["https://example.com/a.png", "https://example.com/b.png"]


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
        "MATTERMOST_ACCESS_TOKEN",
        "MATRIX_ACCESS_TOKEN",
        "ROCKETCHAT_AUTH_TOKEN",
        "ZULIP_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "WEBEX_ACCESS_TOKEN",
        "WHATSAPP_ACCESS_TOKEN",
    ]:
        monkeypatch.delenv(name, raising=False)

    telegram_result = tools.telegram_get_me.func()
    webex_result = tools.webex_list_rooms.func()
    whatsapp_result = tools.whatsapp_list_phone_numbers.func()
    discord_result = tools.discord_get_channel.func(channel_id="channel-1")
    mattermost_result = tools.mattermost_get_me.func()
    matrix_result = tools.matrix_whoami.func()
    rocketchat_result = tools.rocketchat_get_me.func()
    zulip_result = tools.zulip_get_profile.func()

    assert 'provider "telegram"' in telegram_result
    assert "TELEGRAM_BOT_TOKEN" in telegram_result
    assert 'provider "webex"' in webex_result
    assert "WEBEX_ACCESS_TOKEN" in webex_result
    assert 'provider "whatsapp"' in whatsapp_result
    assert "WHATSAPP_ACCESS_TOKEN" in whatsapp_result
    assert 'provider "discord"' in discord_result
    assert "DISCORD_BOT_TOKEN" in discord_result
    assert 'provider "mattermost"' in mattermost_result
    assert "MATTERMOST_ACCESS_TOKEN" in mattermost_result
    assert 'provider "matrix"' in matrix_result
    assert "MATRIX_ACCESS_TOKEN" in matrix_result
    assert 'provider "rocketchat"' in rocketchat_result
    assert "ROCKETCHAT_AUTH_TOKEN" in rocketchat_result
    assert 'provider "zulip"' in zulip_result
    assert "ZULIP_API_KEY" in zulip_result


def test_chat_platform_tools_are_registered_with_metadata():
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = [
        "telegram_get_me",
        "telegram_get_chat",
        "webex_list_rooms",
        "webex_get_room",
        "webex_list_messages",
        "webex_get_message",
        "whatsapp_list_phone_numbers",
        "whatsapp_get_media_url",
        "discord_list_guild_channels",
        "discord_get_channel",
        "discord_get_channel_messages",
        "mattermost_get_me",
        "mattermost_list_teams",
        "mattermost_list_channels",
        "mattermost_list_channel_posts",
        "matrix_whoami",
        "matrix_list_joined_rooms",
        "matrix_get_room_messages",
        "rocketchat_get_me",
        "rocketchat_list_channels",
        "rocketchat_get_channel_history",
        "zulip_get_profile",
        "zulip_list_streams",
        "zulip_get_messages",
    ]
    moderate_names = [
        "telegram_send_message",
        "telegram_delete_message",
        "webex_send_message",
        "webex_delete_message",
        "whatsapp_send_text_message",
        "whatsapp_send_template_message",
        "whatsapp_delete_media",
        "discord_send_channel_message",
        "discord_delete_message",
        "mattermost_create_post",
        "mattermost_delete_post",
        "matrix_send_room_message",
        "matrix_leave_room",
        "rocketchat_post_message",
        "rocketchat_delete_message",
        "zulip_send_message",
        "zulip_delete_message",
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


def test_chat_platform_tool_schemas_hide_runtime_config():
    from nymeria.tools import (
        discord_get_channel_messages,
        mattermost_create_post,
        matrix_send_room_message,
        rocketchat_get_channel_history,
        telegram_send_message,
        webex_send_message,
        whatsapp_send_template_message,
        zulip_send_message,
    )

    assert "config" not in telegram_send_message.args_schema.model_json_schema()["properties"]
    assert "config" not in webex_send_message.args_schema.model_json_schema()["properties"]
    assert "config" not in whatsapp_send_template_message.args_schema.model_json_schema()["properties"]
    assert "config" not in discord_get_channel_messages.args_schema.model_json_schema()["properties"]
    assert "config" not in mattermost_create_post.args_schema.model_json_schema()["properties"]
    assert "config" not in matrix_send_room_message.args_schema.model_json_schema()["properties"]
    assert "config" not in rocketchat_get_channel_history.args_schema.model_json_schema()["properties"]
    assert "config" not in zulip_send_message.args_schema.model_json_schema()["properties"]
