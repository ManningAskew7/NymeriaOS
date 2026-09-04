"""Twitch tools: credential resolution, token minting, honest send, metadata.

Covers the plan's expected behaviors 1-7 (tmp/twitch-restore/plan.md): settings
fallback + setup hints, refresh-grant mint-and-cache with the single 401
re-mint retry, is_sent honesty on twitch_send, broadcaster-token selection,
and the catalog/metadata state of the rewritten family. Transport is faked at
the module's ``_twitch_http`` seam (the one place requests leave the module),
mirroring the ``_request_json`` seam the service-integration tests use.
"""

from pathlib import Path

import pytest

from _service_integration_helpers import (  # type: ignore[import-not-found]
    bind_vault_repo as _use_repo,
    make_vault_repo as _repo,
)

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX = "https://api.twitch.tv/helix"


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text="", content=b""):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.text = text
        self.content = content

    def json(self):
        return self._json


@pytest.fixture(autouse=True)
def _clear_twitch_caches():
    from nymeria.tools import twitch as tools

    tools._TWITCH_TOKEN_CACHE.clear()
    tools._TWITCH_LOGIN_ID_CACHE.clear()
    tools._TWITCH_SELF_ID_CACHE.clear()
    yield
    tools._TWITCH_TOKEN_CACHE.clear()
    tools._TWITCH_LOGIN_ID_CACHE.clear()
    tools._TWITCH_SELF_ID_CACHE.clear()


def _configure_env(monkeypatch, **overrides):
    """Standard env-config shape: refresh-capable bot creds + channel.

    Every key the module reads is listed, including the ones this shape wants
    UNSET (``None`` deletes). A credential a case does not name must mean
    "not configured", never "whatever the ambient environment happens to
    carry": the broadcaster pair was previously unlisted, so a leaked
    ``TWITCH_BROADCASTER_REFRESH_TOKEN`` silently sent the two
    broadcaster-token tests down the mint path (backlog #294).
    """
    values = {
        "TWITCH_CLIENT_ID": "cid",
        "TWITCH_CLIENT_SECRET": "csecret",
        "TWITCH_BOT_ACCESS_TOKEN": None,
        "TWITCH_BOT_REFRESH_TOKEN": "rtok",
        "TWITCH_BOT_USER_ID": "111",
        "TWITCH_BROADCASTER_TOKEN": None,
        "TWITCH_BROADCASTER_REFRESH_TOKEN": None,
        "TWITCH_CHANNEL": "silk",
        **overrides,
    }
    for key, value in values.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def _no_vault(monkeypatch):
    from nymeria.tools import twitch as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)


def _fake_transport(monkeypatch, handler):
    from nymeria.tools import twitch as tools

    calls = []

    def fake(method, url, *, params=None, json_body=None, form_data=None, headers=None):
        call = {
            "method": method,
            "url": url,
            "params": params,
            "json_body": json_body,
            "form_data": form_data,
            "headers": headers,
        }
        calls.append(call)
        return handler(call)

    monkeypatch.setattr(tools, "_twitch_http", fake)
    return calls


def _standard_handler(call):
    """Token mint + user lookups + live stream, the happy-path Helix stub."""
    if call["url"] == TOKEN_URL:
        return FakeResponse(200, {"access_token": "minted-1", "expires_in": 3600})
    if call["url"] == f"{HELIX}/users":
        login = (call["params"] or {}).get("login")
        uid = {"silk": "999", None: "111"}.get(login, "555")
        return FakeResponse(200, {"data": [{"id": uid, "login": login or "botacct"}]})
    if call["url"] == f"{HELIX}/streams":
        return FakeResponse(
            200,
            {"data": [{"title": "T", "game_name": "G", "viewer_count": 3, "started_at": "now"}]},
        )
    raise AssertionError(f"unexpected URL {call['url']}")


# ---------------------------------------------------------------------------
# Behavior 1: settings-only resolution works; unconfigured -> setup hint
# ---------------------------------------------------------------------------


def test_unconfigured_returns_setup_hint_not_traceback(monkeypatch):
    from nymeria.tools import twitch as tools

    _no_vault(monkeypatch)
    monkeypatch.setattr(tools, "_settings_value", lambda name: None)
    result = tools.twitch_get_stream.func(config=None)
    assert result.startswith("[Error]")
    assert "TWITCH_CHANNEL" in result  # channel is checked first, creds after


def test_channel_set_but_no_credentials_hints_at_credentials(monkeypatch):
    from nymeria.tools import twitch as tools

    _no_vault(monkeypatch)
    monkeypatch.setattr(
        tools,
        "_settings_value",
        lambda name: "silk" if name == "twitch_channel" else None,
    )
    result = tools.twitch_get_stream.func(config=None)
    assert result.startswith("[Error]")
    assert "TWITCH_CLIENT_ID" in result


def test_settings_only_credentials_reach_helix(monkeypatch):
    from nymeria.tools import twitch as tools

    _no_vault(monkeypatch)
    _configure_env(monkeypatch)
    calls = _fake_transport(monkeypatch, _standard_handler)

    result = tools.twitch_get_stream.func(config=None)

    assert "LIVE: T" in result and "Viewers: 3" in result
    stream_calls = [c for c in calls if c["url"] == f"{HELIX}/streams"]
    assert stream_calls[0]["headers"]["Client-ID"] == "cid"
    assert stream_calls[0]["headers"]["Authorization"] == "Bearer minted-1"
    assert stream_calls[0]["params"] == {"user_id": "999"}


# ---------------------------------------------------------------------------
# Behavior 2: mint once + cache; 401 -> invalidate, one re-mint, one retry
# ---------------------------------------------------------------------------


def test_refresh_grant_mints_once_and_caches(monkeypatch):
    from nymeria.tools import twitch as tools

    _no_vault(monkeypatch)
    _configure_env(monkeypatch)
    calls = _fake_transport(monkeypatch, _standard_handler)

    tools.twitch_get_stream.func(config=None)
    tools.twitch_get_stream.func(config=None)

    mints = [c for c in calls if c["url"] == TOKEN_URL]
    assert len(mints) == 1
    assert mints[0]["form_data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "rtok",
        "client_id": "cid",
        "client_secret": "csecret",
    }


def test_helix_401_invalidates_and_remints_exactly_once(monkeypatch):
    from nymeria.tools import twitch as tools

    _no_vault(monkeypatch)
    _configure_env(monkeypatch)
    mint_count = {"n": 0}
    stream_count = {"n": 0}

    def handler(call):
        if call["url"] == TOKEN_URL:
            mint_count["n"] += 1
            return FakeResponse(
                200, {"access_token": f"minted-{mint_count['n']}", "expires_in": 3600}
            )
        if call["url"] == f"{HELIX}/users":
            return FakeResponse(200, {"data": [{"id": "999"}]})
        if call["url"] == f"{HELIX}/streams":
            stream_count["n"] += 1
            if stream_count["n"] == 1:
                return FakeResponse(401, text="expired")
            return FakeResponse(200, {"data": []})
        raise AssertionError(call["url"])

    _fake_transport(monkeypatch, handler)

    result = tools.twitch_get_stream.func(config=None)

    assert result == "Stream is offline."
    assert mint_count["n"] == 2  # initial mint + the single 401 re-mint
    assert stream_count["n"] == 2  # one retry, not a loop


def test_persistent_401_surfaces_error_without_looping(monkeypatch):
    from nymeria.tools import twitch as tools

    _no_vault(monkeypatch)
    _configure_env(monkeypatch)
    counts = {"mint": 0, "stream": 0}

    def handler(call):
        if call["url"] == TOKEN_URL:
            counts["mint"] += 1
            return FakeResponse(200, {"access_token": "t", "expires_in": 3600})
        if call["url"] == f"{HELIX}/users":
            return FakeResponse(200, {"data": [{"id": "999"}]})
        counts["stream"] += 1
        return FakeResponse(401, text="still expired")

    _fake_transport(monkeypatch, handler)

    result = tools.twitch_get_stream.func(config=None)

    assert result.startswith("[Error]") and "401" in result
    assert counts["stream"] == 2 and counts["mint"] == 2


def test_static_token_without_refresh_never_mints(monkeypatch):
    from nymeria.tools import twitch as tools

    _no_vault(monkeypatch)
    _configure_env(
        monkeypatch,
        TWITCH_CLIENT_SECRET=None,
        TWITCH_BOT_REFRESH_TOKEN=None,
        TWITCH_BOT_ACCESS_TOKEN="static-tok",
    )
    calls = _fake_transport(monkeypatch, _standard_handler)

    tools.twitch_get_stream.func(config=None)

    assert not [c for c in calls if c["url"] == TOKEN_URL]
    stream_calls = [c for c in calls if c["url"] == f"{HELIX}/streams"]
    assert stream_calls[0]["headers"]["Authorization"] == "Bearer static-tok"


def test_failed_mint_reports_reauth_guidance(monkeypatch):
    from nymeria.tools import twitch as tools

    _no_vault(monkeypatch)
    _configure_env(monkeypatch)

    def handler(call):
        assert call["url"] == TOKEN_URL
        return FakeResponse(400, text='{"message":"Invalid refresh token"}')

    _fake_transport(monkeypatch, handler)

    result = tools.twitch_get_stream.func(config=None)

    assert result.startswith("[Error]") and "OAuth" in result


# ---------------------------------------------------------------------------
# Behavior 3: twitch_send honesty (is_sent / drop_reason)
# ---------------------------------------------------------------------------


def _send_handler(send_result):
    def handler(call):
        if call["url"] == TOKEN_URL:
            return FakeResponse(200, {"access_token": "t", "expires_in": 3600})
        if call["url"] == f"{HELIX}/users":
            return FakeResponse(200, {"data": [{"id": "999"}]})
        if call["url"] == f"{HELIX}/chat/messages":
            return FakeResponse(200, {"data": [send_result]})
        raise AssertionError(call["url"])

    return handler


def test_send_success_reports_sent(monkeypatch):
    from nymeria.tools import twitch as tools

    _no_vault(monkeypatch)
    _configure_env(monkeypatch)
    calls = _fake_transport(
        monkeypatch, _send_handler({"message_id": "m1", "is_sent": True})
    )

    result = tools.twitch_send.func(message="hello chat", config=None)

    assert result.startswith("Sent to #silk")
    send = [c for c in calls if c["url"] == f"{HELIX}/chat/messages"][0]
    assert send["json_body"] == {
        "broadcaster_id": "999",
        "sender_id": "111",
        "message": "hello chat",
    }


def test_send_drop_is_reported_as_failure_with_reason(monkeypatch):
    from nymeria.tools import twitch as tools

    _no_vault(monkeypatch)
    _configure_env(monkeypatch)
    _fake_transport(
        monkeypatch,
        _send_handler(
            {
                "message_id": "",
                "is_sent": False,
                "drop_reason": {"code": "msg_rejected", "message": "AutoMod held this"},
            }
        ),
    )

    result = tools.twitch_send.func(message="spicy message", config=None)

    assert result.startswith("[Error]")
    assert "AutoMod held this" in result


def test_send_splits_long_messages(monkeypatch):
    from nymeria.tools import twitch as tools

    _no_vault(monkeypatch)
    _configure_env(monkeypatch)
    monkeypatch.setattr(tools.time, "sleep", lambda s: None)
    calls = _fake_transport(
        monkeypatch, _send_handler({"message_id": "m", "is_sent": True})
    )

    result = tools.twitch_send.func(message="word " * 250, config=None)

    sends = [c for c in calls if c["url"] == f"{HELIX}/chat/messages"]
    assert len(sends) > 1
    assert all(len(c["json_body"]["message"]) <= 500 for c in sends)
    assert "split into" in result


# ---------------------------------------------------------------------------
# Behavior 4: broadcaster-token selection
# ---------------------------------------------------------------------------


def test_broadcaster_tool_without_broadcaster_token_errors_clearly(monkeypatch):
    from nymeria.tools import twitch as tools

    _no_vault(monkeypatch)
    _configure_env(monkeypatch)
    _fake_transport(monkeypatch, _standard_handler)

    result = tools.twitch_create_poll.func(title="Q?", choices="Yes|No", config=None)

    assert result.startswith("[Error]")
    assert "Broadcaster token not configured" in result


def test_broadcaster_tool_uses_broadcaster_token(monkeypatch):
    from nymeria.tools import twitch as tools

    _no_vault(monkeypatch)
    _configure_env(
        monkeypatch,
        TWITCH_BOT_REFRESH_TOKEN=None,
        TWITCH_BOT_ACCESS_TOKEN="bot-tok",
        TWITCH_BROADCASTER_TOKEN="caster-tok",
    )

    def handler(call):
        if call["url"] == f"{HELIX}/users":
            return FakeResponse(200, {"data": [{"id": "999"}]})
        if call["url"] == f"{HELIX}/polls":
            return FakeResponse(200, {"data": [{"id": "p1", "title": "Q?"}]})
        raise AssertionError(call["url"])

    calls = _fake_transport(monkeypatch, handler)

    result = tools.twitch_create_poll.func(title="Q?", choices="Yes|No", config=None)

    assert "Poll created" in result
    poll = [c for c in calls if c["url"] == f"{HELIX}/polls"][0]
    assert poll["headers"]["Authorization"] == "Bearer caster-tok"
    users = [c for c in calls if c["url"] == f"{HELIX}/users"][0]
    assert users["headers"]["Authorization"] == "Bearer bot-tok"


def test_broadcaster_role_ignores_credentials_the_case_did_not_configure(monkeypatch):
    """Regression for #294: ambient broadcaster creds must not leak into a case.

    The two tests above resolve their credentials from the process
    environment, and previously named only the keys they wanted SET. A
    ``TWITCH_BROADCASTER_REFRESH_TOKEN`` present for any other reason (here,
    set explicitly; in the original incident, merged in from the operator's
    real ``.env.docker`` when pytest imported ``run`` during collection) beat
    the static token and sent the call down the mint path, so the tool
    reported a transport error instead of the setup hint.
    """
    from nymeria.tools import twitch as tools

    monkeypatch.setenv("TWITCH_BROADCASTER_REFRESH_TOKEN", "ambient-refresh")
    monkeypatch.setenv("TWITCH_BROADCASTER_TOKEN", "ambient-static")
    _no_vault(monkeypatch)
    _configure_env(monkeypatch)
    calls = _fake_transport(monkeypatch, _standard_handler)

    result = tools.twitch_create_poll.func(title="Q?", choices="Yes|No", config=None)

    assert "Broadcaster token not configured" in result
    # Minting from the ambient refresh token IS the failure mode. The bot-role
    # mint (rtok, for the broadcaster-id lookup) is expected and must not be
    # confused for it, so match on the grant this test planted.
    assert not [
        call
        for call in calls
        if call["url"] == TOKEN_URL
        and (call["form_data"] or {}).get("refresh_token") == "ambient-refresh"
    ]


# ---------------------------------------------------------------------------
# Request shapes for moderation / chat-action endpoints
# ---------------------------------------------------------------------------


def _mod_handler(monkeypatch, responses):
    """Standard auth/user stubs plus per-URL responses; returns calls list."""
    from nymeria.tools import twitch as tools  # noqa: F401

    def handler(call):
        if call["url"] == TOKEN_URL:
            return FakeResponse(200, {"access_token": "t", "expires_in": 3600})
        if call["url"] == f"{HELIX}/users":
            login = (call["params"] or {}).get("login")
            uid = {"silk": "999"}.get(login, "555")
            return FakeResponse(200, {"data": [{"id": uid}]})
        for url, resp in responses.items():
            if call["url"] == url:
                return resp
        raise AssertionError(call["url"])

    return _fake_transport(monkeypatch, handler)


def test_announce_sends_params_and_json_body(monkeypatch):
    from nymeria.tools import twitch as tools

    _no_vault(monkeypatch)
    _configure_env(monkeypatch)
    calls = _mod_handler(monkeypatch, {f"{HELIX}/chat/announcements": FakeResponse(204)})

    result = tools.twitch_announce.func(message="big news", color="blue", config=None)

    assert "Announcement sent (blue)" in result
    call = [c for c in calls if c["url"] == f"{HELIX}/chat/announcements"][0]
    assert call["method"] == "POST"
    assert call["params"] == {"broadcaster_id": "999", "moderator_id": "111"}
    assert call["json_body"] == {"message": "big news", "color": "blue"}


def test_timeout_resolves_user_and_posts_ban_body(monkeypatch):
    from nymeria.tools import twitch as tools

    _no_vault(monkeypatch)
    _configure_env(monkeypatch)
    calls = _mod_handler(
        monkeypatch, {f"{HELIX}/moderation/bans": FakeResponse(200, {"data": [{}]})}
    )

    result = tools.twitch_timeout.func(
        username="@BadUser", duration=9999, reason="spam", config=None
    )

    assert "Timed out @BadUser for 1800s" in result  # clamped to 1800
    call = [c for c in calls if c["url"] == f"{HELIX}/moderation/bans"][0]
    assert call["json_body"] == {
        "data": {"user_id": "555", "duration": 1800, "reason": "spam"}
    }
    users = [c for c in calls if c["url"] == f"{HELIX}/users"]
    assert any((c["params"] or {}).get("login") == "baduser" for c in users)


def test_delete_message_passes_message_id_param(monkeypatch):
    from nymeria.tools import twitch as tools

    _no_vault(monkeypatch)
    _configure_env(monkeypatch)
    calls = _mod_handler(monkeypatch, {f"{HELIX}/moderation/chat": FakeResponse(204)})

    result = tools.twitch_delete_message.func(message_id="msg-9", config=None)

    assert result == "Deleted message msg-9."
    call = [c for c in calls if c["url"] == f"{HELIX}/moderation/chat"][0]
    assert call["method"] == "DELETE"
    assert call["params"]["message_id"] == "msg-9"


def test_shoutout_maps_429_to_cooldown_message(monkeypatch):
    from nymeria.tools import twitch as tools

    _no_vault(monkeypatch)
    _configure_env(monkeypatch)
    _mod_handler(monkeypatch, {f"{HELIX}/chat/shoutouts": FakeResponse(429)})

    result = tools.twitch_shoutout.func(username="friend", config=None)

    assert "cooldown" in result.lower() and not result.startswith("[Error]")


def test_clip_requires_202_and_returns_edit_url(monkeypatch):
    from nymeria.tools import twitch as tools

    _no_vault(monkeypatch)
    _configure_env(monkeypatch)
    _mod_handler(
        monkeypatch,
        {f"{HELIX}/clips": FakeResponse(202, {"data": [{"id": "c1", "edit_url": "http://e"}]})},
    )

    result = tools.twitch_clip.func(config=None)

    assert "Clip created! ID: c1" in result


def test_get_banned_requests_a_full_page(monkeypatch):
    from nymeria.tools import twitch as tools

    _no_vault(monkeypatch)
    _configure_env(monkeypatch)
    rows = [{"user_login": f"u{i}", "reason": "", "expires_at": None} for i in range(40)]
    calls = _mod_handler(
        monkeypatch, {f"{HELIX}/moderation/banned": FakeResponse(200, {"data": rows})}
    )

    result = tools.twitch_get_banned.func(config=None)

    call = [c for c in calls if c["url"] == f"{HELIX}/moderation/banned"][0]
    assert call["params"]["first"] == 100  # default page is 20; ask for the max
    assert "Banned users (40)" in result and "and 10 more" in result


# ---------------------------------------------------------------------------
# Self-id cache is per token, not per client_id (multi-user, one Twitch app)
# ---------------------------------------------------------------------------


def test_self_id_cache_does_not_leak_between_tokens(monkeypatch):
    from nymeria.tools import twitch as tools

    def per_user_credential(**kwargs):
        user = ((kwargs.get("config") or {}).get("configurable") or {}).get("user_id")
        names = kwargs["field_names"]
        if "client_id" in names:
            return "cid"  # same Twitch application for both users
        if "access_token" in names:
            return f"tok-{user}"
        return None

    monkeypatch.setattr(tools, "_credential_value", per_user_credential)
    monkeypatch.setattr(
        tools,
        "_settings_value",
        lambda name: "silk" if name == "twitch_channel" else None,
    )

    def handler(call):
        token = call["headers"]["Authorization"].removeprefix("Bearer ")
        if call["url"] == f"{HELIX}/users":
            if call["params"] and call["params"].get("login"):
                return FakeResponse(200, {"data": [{"id": "999"}]})
            self_id = {"tok-alice": "111", "tok-bob": "222"}[token]
            return FakeResponse(200, {"data": [{"id": self_id}]})
        if call["url"] == f"{HELIX}/chat/messages":
            return FakeResponse(200, {"data": [{"is_sent": True}]})
        raise AssertionError(call["url"])

    calls = _fake_transport(monkeypatch, handler)

    tools.twitch_send.func(message="hi", config={"configurable": {"user_id": "alice"}})
    tools.twitch_send.func(message="hi", config={"configurable": {"user_id": "bob"}})

    sends = [c for c in calls if c["url"] == f"{HELIX}/chat/messages"]
    assert sends[0]["json_body"]["sender_id"] == "111"
    assert sends[1]["json_body"]["sender_id"] == "222"


# ---------------------------------------------------------------------------
# Vault-first resolution (per-user credential wins over settings)
# ---------------------------------------------------------------------------


def test_vault_credential_wins_over_settings(tmp_path, monkeypatch):
    from nymeria.tools import twitch as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Twitch",
        provider="twitch",
        kind="oauth2",
        allowed_targets=["native_tool:*"],
        secret_fields={"accessToken": "vault-tok", "clientId": "vault-cid"},
        created_by_user_id="alice",
    )
    _configure_env(
        monkeypatch,
        TWITCH_BOT_REFRESH_TOKEN=None,
        TWITCH_BOT_ACCESS_TOKEN="settings-tok",
    )
    calls = _fake_transport(monkeypatch, _standard_handler)

    result = tools.twitch_get_stream.func(config={"configurable": {"user_id": "alice"}})

    assert "LIVE" in result
    stream = [c for c in calls if c["url"] == f"{HELIX}/streams"][0]
    assert stream["headers"]["Authorization"] == "Bearer vault-tok"
    assert stream["headers"]["Client-ID"] == "vault-cid"


# ---------------------------------------------------------------------------
# Bot-account self-resolution (TWITCH_BOT_USER_ID now optional)
# ---------------------------------------------------------------------------


def test_bot_user_id_resolved_from_token_when_unset(monkeypatch):
    from nymeria.tools import twitch as tools

    _no_vault(monkeypatch)
    _configure_env(monkeypatch, TWITCH_BOT_USER_ID=None)

    def handler(call):
        if call["url"] == TOKEN_URL:
            return FakeResponse(200, {"access_token": "t", "expires_in": 3600})
        if call["url"] == f"{HELIX}/users":
            if call["params"] and call["params"].get("login"):
                return FakeResponse(200, {"data": [{"id": "999"}]})
            return FakeResponse(200, {"data": [{"id": "424242"}]})
        if call["url"] == f"{HELIX}/chat/messages":
            return FakeResponse(200, {"data": [{"is_sent": True}]})
        raise AssertionError(call["url"])

    calls = _fake_transport(monkeypatch, handler)

    result = tools.twitch_send.func(message="hi", config=None)

    assert result.startswith("Sent to #silk")
    send = [c for c in calls if c["url"] == f"{HELIX}/chat/messages"][0]
    assert send["json_body"]["sender_id"] == "424242"


# ---------------------------------------------------------------------------
# Behaviors 5 + 7: catalog and metadata state of the rewritten family
# ---------------------------------------------------------------------------


def test_no_tool_carries_the_disabled_prefix():
    from nymeria.tools import twitch as tools

    for t in tools.TWITCH_TOOLS:
        assert "DISABLED" not in t.description, t.name


def test_family_is_22_tools_without_read_chat():
    from nymeria.tools import twitch as tools

    names = {t.name for t in tools.TWITCH_TOOLS}
    assert len(tools.TWITCH_TOOLS) == 22
    assert "twitch_read_chat" not in names
    assert {"twitch_send", "twitch_ban", "twitch_get_stream", "twitch_create_poll"} <= names


# ---------------------------------------------------------------------------
# Behavior: twitch_get_stream_frame, a look at the live broadcast
# ---------------------------------------------------------------------------

JPEG = b"\xff\xd8\xff\xe0" + b"fresh-frame"
CACHED_JPEG = b"\xff\xd8\xff\xe0" + b"cached-frame"
PREVIEW_TEMPLATE = "https://static-cdn.jtvnw.net/previews-ttv/live_user_silk-{width}x{height}.jpg"
CDN = "https://static-cdn.jtvnw.net/"


def _frame_handler(*, live=True, odd=None, base=None, login="silk", streams=None):
    """Helix live stub (with the thumbnail_url Helix really sends), plus the CDN.

    ``odd`` answers any non-standard size (the fresh-render path), ``base``
    the standard 1920x1080 (the cached fallback); ``streams`` overrides the
    whole Helix streams response.
    """
    odd = odd or (lambda: FakeResponse(200, content=JPEG))
    base = base or (lambda: FakeResponse(200, content=CACHED_JPEG))

    def handler(call):
        if call["url"].startswith(CDN):
            assert call["method"] == "GET"
            assert not call["headers"]  # public CDN: no credentials ride along
            size = call["url"].rsplit("-", 1)[1].removesuffix(".jpg")
            return base() if size == "1920x1080" else odd()
        if call["url"] == f"{HELIX}/streams":
            if streams is not None:
                return streams()
            if not live:
                return FakeResponse(200, {"data": []})
            row = {
                "title": "T",
                "game_name": "G",
                "viewer_count": 3,
                "started_at": "now",
                "thumbnail_url": PREVIEW_TEMPLATE,
            }
            if login is not None:
                row["user_login"] = login
            return FakeResponse(200, {"data": [row]})
        return _standard_handler(call)

    return handler


def _cdn_urls(calls):
    return [c["url"] for c in calls if c["url"].startswith(CDN)]


def _cdn_sizes(calls):
    return [u.rsplit("-", 1)[1].removesuffix(".jpg") for u in _cdn_urls(calls)]


def _frame_setup(monkeypatch, tmp_path, handler, *, now=1_700_000_000.0):
    from nymeria.tools import twitch as tools

    _configure_env(monkeypatch)
    _no_vault(monkeypatch)
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(tools.time, "time", lambda: now)
    return tools, _fake_transport(monkeypatch, handler)


def test_stream_frame_attaches_a_fresh_render_from_the_workspace(monkeypatch, tmp_path):
    from nymeria.core.generated_image_context import NATIVE_IMAGE_ARTIFACT_KEY

    tools, calls = _frame_setup(monkeypatch, tmp_path, _frame_handler())
    config = {"configurable": {"user_id": "owner@example.com"}}

    content, artifact = tools.twitch_get_stream_frame.func(config=config)

    assert "Live frame of #silk" in content and "Game: G" in content
    assert "on-demand render" in content
    meta = artifact[NATIVE_IMAGE_ARTIFACT_KEY]
    assert meta["source"] == "twitch_stream_frame"
    assert meta["mime_type"] == "image/jpeg"
    assert meta["native_context_enabled"] is True
    assert f"[attach:{meta['path']}]" in content
    path = Path(meta["path"])
    assert path.is_relative_to(tmp_path.resolve())  # confined: the model may see it
    assert path.read_bytes() == JPEG
    sizes = _cdn_sizes(calls)
    assert len(sizes) == 1 and sizes[0] != "1920x1080"  # never the cached size first
    w, h = (int(x) for x in sizes[0].split("x"))
    assert 1920 - 320 <= w < 1920 and abs(h - w * 9 / 16) < 1


def test_stream_frame_size_cannot_repeat_inside_the_cdn_cache_window(monkeypatch, tmp_path):
    tools, calls = _frame_setup(monkeypatch, tmp_path, _frame_handler())
    t0 = 1_700_000_000.0

    seen = []
    for offset in (0, 1, 299):  # the CDN caches a size for 300 s
        monkeypatch.setattr(tools.time, "time", lambda: t0 + offset)
        tools.twitch_get_stream_frame.func(config=None)
        seen.append(_cdn_sizes(calls)[-1])
    assert len(set(seen)) == 3

    monkeypatch.setattr(tools.time, "time", lambda: t0 + 0.4)  # same second, same instant
    tools.twitch_get_stream_frame.func(config=None)
    assert _cdn_sizes(calls)[-1] == seen[0]

    # The whole window, directly: 300 consecutive seconds give 300 distinct sizes.
    window = {tools._preview_size(t0 + k) for k in range(300)}
    assert len(window) == 300 and (1920, 1080) not in window


@pytest.mark.parametrize(
    "login, expected",
    [
        ("Silk_TV", "silk_tv"),  # Helix login, normalised
        ("silk/../evil?x=1", "silkevilx1"),  # a hostile login can only pick a path segment
        (None, "silk"),  # no Helix login: the configured TWITCH_CHANNEL
    ],
)
def test_stream_frame_url_is_the_cdn_constant_plus_a_sanitised_login(
    monkeypatch, tmp_path, login, expected
):
    tools, calls = _frame_setup(monkeypatch, tmp_path, _frame_handler(login=login))

    tools.twitch_get_stream_frame.func(config=None)

    (url,) = _cdn_urls(calls)
    assert url.startswith(f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{expected}-")
    assert url.endswith(".jpg") and "{" not in url


@pytest.mark.parametrize(
    "streams, needle",
    [
        (lambda: FakeResponse(500, text="helix down"), "500"),
        (lambda: FakeResponse(200, {"data": ["not-a-stream-row"]}), "Error"),
    ],
    ids=["helix-non-200", "malformed-row"],
)
def test_stream_frame_helix_failures_are_error_tuples_without_a_fetch(
    monkeypatch, tmp_path, streams, needle
):
    tools, calls = _frame_setup(monkeypatch, tmp_path, _frame_handler(streams=streams))

    content, artifact = tools.twitch_get_stream_frame.func(config=None)

    assert content.startswith("[Error]") and needle in content
    assert artifact == {}
    assert _cdn_urls(calls) == []


def test_stream_frame_unconfigured_channel_makes_no_request(monkeypatch, tmp_path):
    tools, calls = _frame_setup(monkeypatch, tmp_path, _frame_handler())
    monkeypatch.delenv("TWITCH_CHANNEL", raising=False)

    content, artifact = tools.twitch_get_stream_frame.func(config=None)

    assert content.startswith("[Error]") and "TWITCH_CHANNEL" in content
    assert artifact == {}
    assert calls == []


def test_stream_frame_offline_returns_text_and_no_image(monkeypatch, tmp_path):
    tools, calls = _frame_setup(monkeypatch, tmp_path, _frame_handler(live=False))

    content, artifact = tools.twitch_get_stream_frame.func(config=None)

    assert "offline" in content.lower()
    assert artifact == {}
    assert _cdn_sizes(calls) == []
    assert not list(tmp_path.rglob("*.jpg"))


@pytest.mark.parametrize(
    "odd",
    [
        lambda: FakeResponse(404, text="not found"),
        lambda: FakeResponse(200, content=b"<html>error page</html>"),
        lambda: FakeResponse(200, content=b""),
    ],
    ids=["non-200", "non-jpeg-body", "empty-body"],
)
def test_stream_frame_falls_back_to_the_cached_preview_and_says_so(monkeypatch, tmp_path, odd):
    from nymeria.core.generated_image_context import NATIVE_IMAGE_ARTIFACT_KEY

    tools, calls = _frame_setup(monkeypatch, tmp_path, _frame_handler(odd=odd))

    content, artifact = tools.twitch_get_stream_frame.func(config=None)

    assert "cached preview" in content and "5 minutes" in content
    assert "on-demand" not in content
    assert Path(artifact[NATIVE_IMAGE_ARTIFACT_KEY]["path"]).read_bytes() == CACHED_JPEG
    sizes = _cdn_sizes(calls)
    assert len(sizes) == 2 and sizes[1] == "1920x1080" and sizes[0] != "1920x1080"


def test_stream_frame_both_paths_failing_is_an_honest_error(monkeypatch, tmp_path):
    fail = lambda: FakeResponse(503, text="cdn down")  # noqa: E731
    tools, _ = _frame_setup(monkeypatch, tmp_path, _frame_handler(odd=fail, base=fail))

    content, artifact = tools.twitch_get_stream_frame.func(config=None)

    assert content.startswith("[Error]") and "preview" in content
    assert artifact == {}
    assert not list(tmp_path.rglob("*.jpg"))


def test_stream_frame_is_a_safe_info_tool_bound_to_the_twitch_credential():
    from nymeria.tools import twitch as tools
    from nymeria.tools.metadata import SecurityLevel, get_tool_metadata

    assert get_tool_metadata("twitch_get_stream_frame").security_level == SecurityLevel.SAFE
    assert "twitch_get_stream_frame" in tools._TWITCH.tools
    assert tools.twitch_get_stream_frame.response_format == "content_and_artifact"


def test_get_stream_names_the_cached_preview_url(monkeypatch):
    tools, calls = _frame_setup(monkeypatch, Path("/nonexistent-unused"), _frame_handler())

    result = tools.twitch_get_stream.func(config=None)

    assert "LIVE: T" in result
    assert "Preview: https://static-cdn.jtvnw.net/previews-ttv/live_user_silk-1920x1080.jpg" in result
    assert _cdn_urls(calls) == []  # names it, never fetches it


def test_security_metadata_preserved():
    from nymeria.tools.metadata import SecurityLevel, get_tool_metadata

    assert get_tool_metadata("twitch_ban").security_level == SecurityLevel.SENSITIVE
    assert get_tool_metadata("twitch_get_stream").security_level == SecurityLevel.SAFE
    assert get_tool_metadata("twitch_send").security_level == SecurityLevel.MODERATE


def test_tools_execute_without_any_bot_process(monkeypatch):
    """The old runtime facade is gone: no registration precondition remains."""
    import nymeria.tools.twitch as tools

    assert not hasattr(tools, "_get_runtime")
    _no_vault(monkeypatch)
    _configure_env(monkeypatch)
    _fake_transport(monkeypatch, _standard_handler)
    assert "LIVE" in tools.twitch_get_stream.func(config=None)
