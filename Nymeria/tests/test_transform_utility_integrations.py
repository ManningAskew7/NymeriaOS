import base64
import gzip
import hmac
import hashlib
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


def test_datetime_tools_transform_dates():
    from nymeria.tools import transform_utility_integrations as tools

    added = tools.datetime_add.func(
        date_value="2026-05-14T10:30:00",
        amount=2,
        unit="days",
        timezone_name="UTC",
        output_format="%Y-%m-%d %H:%M",
    )
    rounded = tools.datetime_round.func(
        date_value="2026-05-14T10:30:15Z",
        unit="hour",
        mode="floor",
        timezone_name="UTC",
    )
    between = tools.datetime_between.func(
        start_date="2026-05-14T00:00:00Z",
        end_date="2026-05-15T12:00:00Z",
        unit="hours",
        timezone_name="UTC",
    )
    extracted = tools.datetime_extract.func(
        date_value="2026-05-14T10:30:00Z",
        part="quarter",
        timezone_name="UTC",
    )

    assert added == "2026-05-16 10:30"
    assert rounded == "2026-05-14T10:00:00+00:00"
    assert between == "36.0"
    assert extracted == "2"


def test_crypto_hash_and_random_are_local():
    from nymeria.tools import transform_utility_integrations as tools

    assert tools.crypto_hash_text.func("hello", algorithm="sha256") == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e"
        "1b161e5c1fa7425e73043362938b9824"
    )
    random_value = tools.crypto_generate_random.func(kind="ascii", length=24)
    assert len(random_value) == 24
    assert random_value.isalnum()


def test_totp_generate_and_verify_use_env_secret(monkeypatch):
    from nymeria.tools import transform_utility_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("TOTP_SECRET", "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")

    generated = json.loads(
        tools.totp_generate_code.func(
            timestamp=59,
            period=30,
            digits=8,
            algorithm="sha1",
        )
    )
    verified = json.loads(
        tools.totp_verify_code.func(
            code="94287082",
            timestamp=59,
            period=30,
            digits=8,
            algorithm="sha1",
            window=0,
        )
    )

    assert generated["code"] == "94287082"
    assert generated["valid_from"] == 30
    assert generated["valid_until"] == 60
    assert verified["valid"] is True
    assert verified["counter_offset"] == 0


def test_crypto_hmac_uses_vault_secret(tmp_path, monkeypatch):
    from nymeria.tools import transform_utility_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Crypto",
        provider="crypto",
        kind="api_key",
        allowed_targets=["native_tool:crypto_hmac_text"],
        secret_fields={"hmacSecret": "secret"},
        created_by_user_id="alice",
    )

    result = tools.crypto_hmac_text.func(
        "hello",
        algorithm="sha256",
        encoding="base64",
        config={"configurable": {"user_id": "alice"}},
    )

    expected = hmac.new(b"secret", b"hello", hashlib.sha256).digest()
    assert result == base64.b64encode(expected).decode("ascii")


def test_jwt_sign_verify_and_decode_use_env_secret(monkeypatch):
    from nymeria.tools import transform_utility_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("JWT_SECRET", "jwt-secret-value-with-at-least-32-bytes")
    token = tools.jwt_sign_claims.func(
        claims_json='{"sub": "alice", "role": "admin"}',
        algorithm="HS256",
    )
    verified = json.loads(tools.jwt_verify_token.func(token=token, algorithm="HS256"))
    decoded = json.loads(tools.jwt_decode_token.func(token=token, complete=True))

    assert verified["payload"]["sub"] == "alice"
    assert verified["payload"]["role"] == "admin"
    assert decoded["header"]["alg"] == "HS256"
    assert decoded["payload"]["sub"] == "alice"


def test_compression_tools_round_trip_text_and_zip():
    from nymeria.tools import transform_utility_integrations as tools

    gzip_result = json.loads(tools.compression_gzip_text.func("hello zip"))
    assert gzip_result["format"] == "gzip"
    assert gzip.decompress(base64.b64decode(gzip_result["data"])).decode() == "hello zip"
    assert tools.compression_gunzip_text.func(gzip_result["data"]) == "hello zip"

    zip_result = json.loads(
        tools.compression_zip_text_files.func('{"a.txt": "alpha", "dir/b.txt": "beta"}')
    )
    unzipped = json.loads(tools.compression_unzip_text_files.func(zip_result["data"]))
    assert unzipped["files"]["a.txt"]["text"] == "alpha"
    assert unzipped["files"]["dir/b.txt"]["text"] == "beta"


def test_transform_secret_tools_return_setup_hints(monkeypatch):
    from nymeria.tools import transform_utility_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setattr(tools, "_settings_value", lambda name: None)

    hmac_result = tools.crypto_hmac_text.func("hello")
    jwt_result = tools.jwt_verify_token.func(token="not-a-token")

    assert "No Crypto credential found" in hmac_result
    assert "CRYPTO_HMAC_SECRET" in hmac_result
    assert "native_tool:crypto_hmac_text" in hmac_result
    assert "No JWT credential found" in jwt_result
    assert "JWT_SECRET" in jwt_result
    assert "native_tool:jwt_verify_token" in jwt_result


def test_transform_utility_tools_are_registered_with_metadata():
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = {
        "datetime_current",
        "datetime_add",
        "datetime_subtract",
        "datetime_format",
        "datetime_between",
        "datetime_extract",
        "datetime_round",
        "crypto_hash_text",
        "crypto_generate_random",
        "jwt_decode_token",
        "compression_gzip_text",
        "compression_gunzip_text",
        "compression_zip_text_files",
        "compression_unzip_text_files",
    }
    moderate_names = {
        "crypto_hmac_text",
        "totp_generate_code",
        "totp_verify_code",
        "crypto_sign_text",
        "jwt_sign_claims",
        "jwt_verify_token",
    }

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


def test_transform_tool_schemas_hide_runtime_config():
    from nymeria.tools.transform_utility_integrations import (
        crypto_hmac_text,
        crypto_sign_text,
        jwt_sign_claims,
        jwt_verify_token,
        totp_generate_code,
        totp_verify_code,
    )

    assert "config" not in crypto_hmac_text.args_schema.model_json_schema()["properties"]
    assert "config" not in crypto_sign_text.args_schema.model_json_schema()["properties"]
    assert "config" not in jwt_sign_claims.args_schema.model_json_schema()["properties"]
    assert "config" not in jwt_verify_token.args_schema.model_json_schema()["properties"]
    assert "config" not in totp_generate_code.args_schema.model_json_schema()["properties"]
    assert "config" not in totp_verify_code.args_schema.model_json_schema()["properties"]
