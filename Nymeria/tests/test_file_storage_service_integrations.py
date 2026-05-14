import json

import pytest
from cryptography.fernet import Fernet

from nymeria.core.accounts import AccountsRepo
from nymeria.core.credential_vault import CredentialVaultRepo


@pytest.fixture(autouse=True)
def clear_settings_cache():
    from nymeria.config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _repo(tmp_path, monkeypatch) -> CredentialVaultRepo:
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())
    db_path = tmp_path / "accounts.db"
    accounts = AccountsRepo(db_path)
    accounts.create_user("alice", "alice@example.com", "Alice")
    return CredentialVaultRepo(db_path)


def _use_repo(monkeypatch, repo: CredentialVaultRepo) -> None:
    import nymeria.core.credential_vault as credential_vault

    monkeypatch.setattr(credential_vault, "get_credential_vault_repo", lambda: repo)


def test_dropbox_list_folder_uses_env_token(monkeypatch):
    from nymeria.tools import file_storage_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "drop-token")
    monkeypatch.setenv("DROPBOX_API_BASE_URL", "https://drop.example/2")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"entries": [{"name": "Docs", ".tag": "folder"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.dropbox_list_folder.func(path="/work", recursive=True, include_deleted=False, limit=25)
    )

    assert result == [{"name": "Docs", ".tag": "folder"}]
    assert captured["method"] == "POST"
    assert captured["url"] == "https://drop.example/2/files/list_folder"
    assert captured["headers"]["Authorization"] == "Bearer drop-token"
    assert captured["json_body"]["path"] == "/work"
    assert captured["json_body"]["recursive"] is True
    assert captured["json_body"]["limit"] == 25


def test_dropbox_upload_uses_vault_token(tmp_path, monkeypatch):
    from nymeria.tools import file_storage_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Dropbox",
        provider="dropbox_oauth2",
        kind="oauth",
        allowed_targets=["native_tool:dropbox_upload_text_file"],
        secret_fields={
            "accessToken": "drop-token",
            "content_base_url": "https://content.drop.example/2",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"name": "note.txt", "path_display": "/note.txt"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.dropbox_upload_text_file.func(
            path="/note.txt",
            content="hello",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["name"] == "note.txt"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://content.drop.example/2/files/upload"
    assert captured["headers"]["Dropbox-API-Arg"] == json.dumps({"path": "/note.txt", "mode": "overwrite"})
    assert captured["data"] == b"hello"


def test_nextcloud_list_folder_uses_env_basic_auth(monkeypatch):
    from nymeria.tools import file_storage_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("NEXTCLOUD_WEBDAV_URL", "https://cloud.example/remote.php/webdav")
    monkeypatch.setenv("NEXTCLOUD_USERNAME", "ada")
    monkeypatch.setenv("NEXTCLOUD_PASSWORD", "secret")
    captured = {}
    xml = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:">
  <d:response>
    <d:href>/remote.php/webdav/docs/</d:href>
    <d:propstat><d:prop>
      <d:displayname>docs</d:displayname>
      <d:resourcetype><d:collection/></d:resourcetype>
      <d:getlastmodified>Thu, 14 May 2026 00:00:00 GMT</d:getlastmodified>
    </d:prop></d:propstat>
  </d:response>
</d:multistatus>"""

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return xml, {}

    monkeypatch.setattr(tools, "_request_text", fake_request)

    result = json.loads(tools.nextcloud_list_folder.func(path="/docs", depth=1))

    assert result[0]["name"] == "docs"
    assert result[0]["type"] == "folder"
    assert captured["method"] == "PROPFIND"
    assert captured["url"] == "https://cloud.example/remote.php/webdav/docs"
    assert captured["headers"]["Depth"] == "1"
    assert captured["auth"] is not None


def test_nextcloud_get_user_uses_vault_access_token(tmp_path, monkeypatch):
    from nymeria.tools import file_storage_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Nextcloud",
        provider="nextcloud",
        kind="oauth",
        allowed_targets=["native_tool:*"],
        secret_fields={"webDavUrl": "https://cloud.example/remote.php/webdav", "accessToken": "nc-token"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"ocs": {"data": {"id": "ada"}}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.nextcloud_get_user.func(user_id="ada", config={"configurable": {"user_id": "alice"}})
    )

    assert result["ocs"]["data"]["id"] == "ada"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://cloud.example/ocs/v1.php/cloud/users/ada"
    assert captured["headers"]["Authorization"] == "Bearer nc-token"
    assert captured["params"]["format"] == "json"


def test_s3_upload_text_object_uses_fake_client(monkeypatch):
    from nymeria.tools import file_storage_service_integrations as tools

    captured = {}

    class FakeS3Client:
        def put_object(self, **kwargs):
            captured.update(kwargs)
            return {"ETag": '"etag"'}

    monkeypatch.setattr(tools, "_s3_client", lambda tool_name, config: (FakeS3Client(), None))

    result = json.loads(
        tools.s3_upload_text_object.func(
            bucket="bucket",
            key="notes/today.txt",
            content="hello",
            content_type="text/plain",
        )
    )

    assert result["etag"] == '"etag"'
    assert captured["Bucket"] == "bucket"
    assert captured["Key"] == "notes/today.txt"
    assert captured["Body"] == b"hello"
    assert captured["ContentType"] == "text/plain"


def test_file_storage_missing_credentials_return_setup_hints(monkeypatch):
    from nymeria.tools import file_storage_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setattr(tools, "_settings_value", lambda name: "https://cloud.example/remote.php/webdav" if name == "nextcloud_webdav_url" else None)

    dropbox = tools.dropbox_get_current_account.func()
    nextcloud = tools.nextcloud_list_users.func()
    s3 = tools.s3_list_buckets.func()

    assert "No Dropbox credential found" in dropbox
    assert "DROPBOX_ACCESS_TOKEN" in dropbox
    assert "native_tool:dropbox_get_current_account" in dropbox
    assert "No Nextcloud credential found" in nextcloud
    assert "NEXTCLOUD_USERNAME + NEXTCLOUD_PASSWORD or NEXTCLOUD_ACCESS_TOKEN" in nextcloud
    assert "No S3 credential found" in s3
    assert "AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY" in s3


def test_file_storage_tools_are_registered_with_metadata():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = {
        "dropbox_get_current_account",
        "dropbox_get_metadata",
        "dropbox_list_folder",
        "dropbox_search",
        "dropbox_download_file",
        "nextcloud_list_folder",
        "nextcloud_download_file",
        "nextcloud_list_users",
        "nextcloud_get_user",
        "s3_list_buckets",
        "s3_list_objects",
        "s3_get_object_text",
    }
    moderate_names = {
        "dropbox_upload_text_file",
        "dropbox_create_folder",
        "dropbox_copy_path",
        "dropbox_move_path",
        "dropbox_delete_path",
        "nextcloud_upload_text_file",
        "nextcloud_create_folder",
        "nextcloud_copy_path",
        "nextcloud_move_path",
        "nextcloud_delete_path",
        "s3_upload_text_object",
        "s3_copy_object",
        "s3_delete_object",
        "s3_create_folder",
        "s3_create_bucket",
        "s3_delete_bucket",
    }

    for name in safe_names:
        assert name in OPTIONAL_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.INTEGRATIONS
        assert metadata.security_level == SecurityLevel.SAFE

    for name in moderate_names:
        assert name in OPTIONAL_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.INTEGRATIONS
        assert metadata.security_level == SecurityLevel.MODERATE


def test_file_storage_tool_schemas_hide_runtime_config():
    from nymeria.tools.file_storage_service_integrations import (
        dropbox_upload_text_file,
        nextcloud_list_folder,
        s3_upload_text_object,
    )

    assert "config" not in dropbox_upload_text_file.args_schema.model_json_schema()["properties"]
    assert "config" not in nextcloud_list_folder.args_schema.model_json_schema()["properties"]
    assert "config" not in s3_upload_text_object.args_schema.model_json_schema()["properties"]
