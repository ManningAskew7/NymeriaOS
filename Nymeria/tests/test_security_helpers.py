"""Focused coverage for security-adjacent helper modules."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

import nymeria.config as config_module
import nymeria.core.secrets as secrets
from nymeria.core import http_policy
from nymeria.core.validator import CodeValidator


def test_secrets_encrypt_decrypt_roundtrip_reload_and_wrong_key(monkeypatch):
    first_key = secrets.generate_key()
    second_key = secrets.generate_key()
    monkeypatch.setenv(secrets.SECRETS_KEY_ENV_VAR, first_key)

    ciphertext = secrets.encrypt("token-\u2603")

    assert ciphertext != "token-\u2603"
    assert secrets.decrypt(ciphertext) == "token-\u2603"

    monkeypatch.setenv(secrets.SECRETS_KEY_ENV_VAR, second_key)
    with pytest.raises(secrets.InvalidToken):
        secrets.decrypt(ciphertext)


def test_secrets_missing_invalid_and_type_errors(monkeypatch):
    monkeypatch.delenv(secrets.SECRETS_KEY_ENV_VAR, raising=False)

    assert secrets.has_secrets_key() is False
    with pytest.raises(secrets.SecretsKeyMissing, match=secrets.SECRETS_KEY_ENV_VAR):
        secrets.encrypt("value")

    monkeypatch.setenv(secrets.SECRETS_KEY_ENV_VAR, "not-a-fernet-key")
    assert secrets.has_secrets_key() is True
    with pytest.raises(secrets.SecretsKeyInvalid):
        secrets.decrypt("not-a-token")

    with pytest.raises(TypeError):
        secrets.encrypt(b"bytes")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        secrets.decrypt(b"bytes")  # type: ignore[arg-type]


def test_http_audit_writes_redacted_jsonl(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    monkeypatch.setattr(
        config_module,
        "get_settings",
        lambda: SimpleNamespace(audit_log_enabled=True, logs_dir=logs_dir),
    )

    http_policy.audit_http_event(
        {
            "tool": "http_request",
            "url": "https://api.example.com",
            "headers": {
                "Authorization": "Bearer secret-token",
                "X-API-Key": "sk-test_secretsecretsecret",
                "X-Trace": "public",
            },
            "body": {"note": "github token ghp_abcdefghijklmnopqrstuvwxyz"},
        }
    )

    log_files = list(logs_dir.glob("audit_*.jsonl"))
    assert len(log_files) == 1
    entry = json.loads(log_files[0].read_text(encoding="utf-8"))

    assert entry["event_type"] == "http_tool_call"
    assert entry["tool"] == "http_request"
    assert entry["headers"]["Authorization"] == "[REDACTED]"
    assert entry["headers"]["X-API-Key"] == "[REDACTED]"
    assert entry["headers"]["X-Trace"] == "public"
    assert entry["body"]["note"] == "github token [REDACTED]"
    assert "timestamp" in entry


def test_http_audit_write_failure_logs_error(monkeypatch, tmp_path, caplog):
    blocking_file = tmp_path / "not-a-directory"
    blocking_file.write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        config_module,
        "get_settings",
        lambda: SimpleNamespace(
            audit_log_enabled=True,
            logs_dir=blocking_file / "audit",
        ),
    )

    caplog.set_level(logging.ERROR, logger="nymeria.core.http_policy")

    http_policy.audit_http_event({"tool": "http_request"})

    assert "Failed to write HTTP audit event" in caplog.text


def test_requests_get_with_policy_blocks_private_redirect():
    class FakeResponse:
        status_code = 302
        headers = {"location": "https://127.0.0.1/admin"}
        url = "https://api.example.com/start"
        is_redirect = True

    class FakeSession:
        def __init__(self):
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            return FakeResponse()

    session = FakeSession()

    with pytest.raises(http_policy.HTTPPolicyViolation) as exc_info:
        http_policy.requests_get_with_policy(
            "https://api.example.com/start",
            session=session,
            config=http_policy.HTTPPolicyConfig(resolve_dns=False),
        )

    assert session.calls == 1
    assert exc_info.value.decision.reason == "loopback_network"
    assert exc_info.value.redirect_chain[0]["redirect_url"] == "https://127.0.0.1/admin"


def test_validate_http_egress_url_blocks_literal_private_base_url():
    with pytest.raises(ValueError, match="blocked by HTTP egress policy"):
        http_policy.validate_http_egress_url(
            "http://127.0.0.1:8000/api",
            label="base URL",
            resolve_dns=False,
        )


def test_validator_accepts_sync_and_async_tool_definitions(tmp_path):
    validator = CodeValidator(tmp_path)

    sync_valid, sync_msg = validator.validate_tool_definition(
        "from langchain_core.tools import tool\n\n"
        "@tool\n"
        "def lookup_price(symbol: str) -> str:\n"
        "    return symbol\n"
    )
    async_valid, async_msg = validator.validate_tool_definition(
        "from langchain_core.tools import tool\n\n"
        "@tool\n"
        "async def fetch_price(symbol: str) -> str:\n"
        "    return symbol\n"
    )

    assert sync_valid is True
    assert "lookup_price" in sync_msg
    assert async_valid is True
    assert "fetch_price" in async_msg


def test_validator_rejects_bad_syntax_missing_tool_and_disallowed_path(tmp_path):
    validator = CodeValidator(tmp_path)
    tools_dir = tmp_path / "nymeria" / "tools"
    tool_file = tools_dir / "example_tool.py"

    valid_syntax, syntax_msg = validator.validate_python_syntax("def broken(:\n")
    valid_tool, tool_msg = validator.validate_modification(
        tool_file,
        "def helper() -> str:\n"
        "    return 'missing decorator'\n",
    )
    valid_path, path_msg = validator.validate_modification(
        tmp_path / "outside.py",
        "@tool\n"
        "def outside() -> str:\n"
        "    return 'nope'\n",
    )

    assert valid_syntax is False
    assert "Syntax error" in syntax_msg
    assert valid_tool is False
    assert "No @tool decorated function found" in tool_msg
    assert valid_path is False
    assert "Path not allowed" in path_msg
