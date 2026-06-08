import json

import pytest
from cryptography.fernet import Fernet

from nymeria.core.accounts import AccountsRepo
from nymeria.core.credential_vault import CredentialVaultRepo


@pytest.fixture(autouse=True)
def clear_settings_cache():
    from nymeria.config.settings import get_settings
    from nymeria.tools import media_discovery_service_integrations as tools

    get_settings.cache_clear()
    tools._SPOTIFY_TOKEN_CACHE.clear()
    yield
    get_settings.cache_clear()
    tools._SPOTIFY_TOKEN_CACHE.clear()


def _repo(tmp_path, monkeypatch) -> CredentialVaultRepo:
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())
    db_path = tmp_path / "accounts.db"
    accounts = AccountsRepo(db_path)
    accounts.create_user("alice", "alice@example.com", "Alice")
    return CredentialVaultRepo(db_path)


def _use_repo(monkeypatch, repo: CredentialVaultRepo) -> None:
    import nymeria.core.credential_vault as credential_vault

    monkeypatch.setattr(credential_vault, "get_credential_vault_repo", lambda: repo)


def test_google_books_search_uses_optional_vault_key(tmp_path, monkeypatch):
    from nymeria.tools import media_discovery_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Google Books",
        provider="google_books",
        kind="api_key",
        allowed_targets=["native_tool:google_books_search"],
        secret_fields={"apiKey": "books-key", "baseUrl": "https://books.example/v1"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"items": [{"id": "volume-1"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.google_books_search.func(
            query="systems design",
            limit=4,
            language="en",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["items"][0]["id"] == "volume-1"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://books.example/v1/volumes"
    assert captured["params"]["q"] == "systems design"
    assert captured["params"]["maxResults"] == 4
    assert captured["params"]["langRestrict"] == "en"
    assert captured["params"]["key"] == "books-key"


def test_youtube_search_uses_env_api_key(monkeypatch):
    from nymeria.tools import media_discovery_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("YOUTUBE_API_KEY", "youtube-key")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"items": [{"id": {"videoId": "video-1"}}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.youtube_search.func(
            query="nymeria",
            search_type="video,channel",
            order="date",
            limit=7,
        )
    )

    assert result["items"][0]["id"]["videoId"] == "video-1"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://www.googleapis.com/youtube/v3/search"
    assert captured["params"]["type"] == "video,channel"
    assert captured["params"]["order"] == "date"
    assert captured["params"]["maxResults"] == 7
    assert captured["params"]["key"] == "youtube-key"


def test_youtube_missing_key_returns_setup_hint(monkeypatch):
    from nymeria.tools import media_discovery_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setattr(tools, "_settings_value", lambda name: None)
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)

    result = tools.youtube_get_videos.func(video_ids="video-1")

    assert result.startswith("[Error]: No ")
    assert "youtube_get_videos" in result


def test_spotify_search_uses_client_credentials(monkeypatch):
    from nymeria.tools import media_discovery_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "spotify-id")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "spotify-secret")
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        if url == "https://accounts.spotify.com/api/token":
            return {"access_token": "spotify-token", "expires_in": 3600}
        return {"tracks": {"items": [{"id": "track-1"}]}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.spotify_search.func(
            query="Miles Davis",
            types="track,artist",
            market="US",
            limit=5,
        )
    )

    assert result["tracks"]["items"][0]["id"] == "track-1"
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "https://accounts.spotify.com/api/token"
    assert calls[0]["form_data"] == {"grant_type": "client_credentials"}
    assert calls[0]["headers"]["Authorization"].startswith("Basic ")
    assert calls[1]["method"] == "GET"
    assert calls[1]["url"] == "https://api.spotify.com/v1/search"
    assert calls[1]["headers"]["Authorization"] == "Bearer spotify-token"
    assert calls[1]["params"]["type"] == "track,artist"
    assert calls[1]["params"]["market"] == "US"
    assert calls[1]["params"]["limit"] == 5


def test_spotify_get_track_prefers_vault_access_token(tmp_path, monkeypatch):
    from nymeria.tools import media_discovery_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Spotify",
        provider="spotify",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={"accessToken": "vault-token", "apiUrl": "https://spotify.example/v1"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"id": "track-1"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.spotify_get_track.func(
            track_id="track-1",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["id"] == "track-1"
    assert captured["url"] == "https://spotify.example/v1/tracks/track-1"
    assert captured["headers"]["Authorization"] == "Bearer vault-token"


def test_media_discovery_registration_and_metadata():
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.media_discovery_service_integrations import MEDIA_DISCOVERY_SERVICE_TOOLS
    from nymeria.tools.metadata import SecurityLevel, get_tool_metadata

    names = {tool.name for tool in MEDIA_DISCOVERY_SERVICE_TOOLS}
    assert {
        "google_books_search",
        "youtube_search",
        "spotify_search",
        "spotify_get_playlist",
    } <= names
    assert names <= set(CATALOG_TOOLS)
    assert get_tool_metadata("youtube_search").security_level == SecurityLevel.SAFE
    assert get_tool_metadata("spotify_get_playlist").security_level == SecurityLevel.SAFE


def test_media_discovery_tool_schemas_hide_config():
    from nymeria.tools import media_discovery_service_integrations as tools

    for tool in tools.MEDIA_DISCOVERY_SERVICE_TOOLS:
        schema = tool.args_schema.model_json_schema()
        assert "config" not in schema.get("properties", {})
