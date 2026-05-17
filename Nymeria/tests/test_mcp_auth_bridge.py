from __future__ import annotations

import json
import stat
import time
from types import SimpleNamespace

from nymeria.core import mcp_auth_bridge
from nymeria.tools.definitions.mcp_schema import MCPServerDefinition


def _gmail_server() -> MCPServerDefinition:
    return MCPServerDefinition(
        id="gmail-test",
        name="Gmail",
        transport="stdio",
        server_command="npx",
        server_args=["@gongrzhe/server-gmail-autoauth-mcp"],
    )


def test_gmail_mcp_preset_sets_paths_and_exports_token(tmp_path, monkeypatch):
    oauth_file = tmp_path / "google_credentials.json"
    oauth_file.write_text(json.dumps({"installed": {"client_id": "id"}}))
    cache = {
        "accounts": {
            "acct": {
                "email": "gmail@example.com",
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "scopes": [
                    "https://www.googleapis.com/auth/gmail.modify",
                    "https://www.googleapis.com/auth/gmail.settings.basic",
                ],
                "expires_at": time.time() + 3600,
            }
        }
    }

    monkeypatch.setattr(
        mcp_auth_bridge,
        "get_settings",
        lambda: SimpleNamespace(data_dir=tmp_path),
    )
    monkeypatch.setattr(
        mcp_auth_bridge.auth_utils,
        "get_google_credentials_path",
        lambda: str(oauth_file),
    )
    monkeypatch.setattr(
        mcp_auth_bridge.auth_utils,
        "load_token_cache",
        lambda user_id, cache_filename: cache if cache_filename == "google_gmail.json" else {},
    )

    logs: list[str] = []
    defn = mcp_auth_bridge.apply_mcp_auth_presets(
        _gmail_server(),
        user_id="alice",
        log_sink=logs,
    )

    credentials_path = tmp_path / "auth_tokens" / "alice" / "mcp" / "gmail" / "credentials.json"
    assert defn.env_vars["GMAIL_OAUTH_PATH"] == str(oauth_file)
    assert defn.env_vars["GMAIL_CREDENTIALS_PATH"] == str(credentials_path)
    exported = json.loads(credentials_path.read_text())
    assert exported["access_token"] == "access-token"
    assert exported["refresh_token"] == "refresh-token"
    assert exported["scope"] == " ".join(cache["accounts"]["acct"]["scopes"])
    assert stat.S_IMODE(credentials_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(credentials_path.stat().st_mode) == 0o600
    assert any("Exported Google Gmail auth connection" in line for line in logs)


def test_gmail_mcp_preset_does_not_export_without_gmail_scopes(tmp_path, monkeypatch):
    oauth_file = tmp_path / "google_credentials.json"
    oauth_file.write_text("{}")
    cache = {
        "accounts": {
            "acct": {
                "email": "docs@example.com",
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "scopes": ["https://www.googleapis.com/auth/documents"],
                "expires_at": time.time() + 3600,
            }
        }
    }

    monkeypatch.setattr(
        mcp_auth_bridge,
        "get_settings",
        lambda: SimpleNamespace(data_dir=tmp_path),
    )
    monkeypatch.setattr(
        mcp_auth_bridge.auth_utils,
        "get_google_credentials_path",
        lambda: str(oauth_file),
    )
    monkeypatch.setattr(
        mcp_auth_bridge.auth_utils,
        "load_token_cache",
        lambda user_id, cache_filename: cache if cache_filename == "google_docs.json" else {},
    )

    logs: list[str] = []
    defn = mcp_auth_bridge.apply_mcp_auth_presets(
        _gmail_server(),
        user_id="alice",
        log_sink=logs,
    )

    credentials_path = tmp_path / "auth_tokens" / "alice" / "mcp" / "gmail" / "credentials.json"
    assert defn.env_vars["GMAIL_CREDENTIALS_PATH"] == str(credentials_path)
    assert not credentials_path.exists()
    assert any("missing Gmail scopes" in line for line in logs)
    assert any("gmail_auth_start" in line for line in logs)
