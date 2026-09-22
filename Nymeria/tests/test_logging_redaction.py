"""Regression tests for logging secret redaction."""

from __future__ import annotations

import logging

from nymeria.config.logging_config import (
    _REDACTION_CHUNK_SIZE,
    _TokenRedactingFilter,
    NymeriaFormatter,
    _redact_text,
)
from nymeria.core.request_context import reset_request_id, set_request_id


def test_redact_text_masks_common_provider_tokens():
    raw = (
        "nym_abcdefghijklmnopqrstuvwxyz123456 "
        "sk-ant-abcdefghijklmnopqrstuvwxyz123456 "
        "sk-proj-abcdefghijklmnopqrstuvwxyz123456 "
        "ghp_abcdefghijklmnopqrstuvwxyz123456 "
        "github_pat_abcdefghijklmnopqrstuvwxyz123456 "
        "xoxb-abcdefghijklmnopqrstuvwxyz123456 "
        "AIzaabcdefghijklmnopqrstuvwxyz123456 "
        "AKIAABCDEFGHIJKLMNOP "
        "SG.abcdefghijklmnopqrstuvwxyz.abcdefghijklmnopqrstuvwxyz "
        "hf_abcdefghijklmnopqrstuvwxyz123456 "
        "r8_abcdefghijklmnopqrstuvwxyz123456 "
        "npm_abcdefghijklmnopqrstuvwxyz123456 "
        "pypi-abcdefghijklmnopqrstuvwxyz123456 "
        "cpx-abcdefghijklmnopqrstuvwxyz123456"
    )

    redacted = _redact_text(raw)

    assert "abcdefghijklmnopqrstuvwxyz123456" not in redacted
    assert "AKIAABCDEFGHIJKLMNOP" not in redacted
    assert "nym_ab...3456" in redacted
    assert "sk-ant...3456" in redacted
    assert "github...3456" in redacted


def test_redact_text_masks_bearer_jwt_assignments_and_urls():
    jwt = "eyJabcdefghijkl.mnOPQRSTUVWXYZ.rsTUVWXYZ1234"
    raw = (
        f"Authorization: Bearer {jwt} "
        "OPENAI_API_KEY=sk-openai-secret-value-123456 "
        '"api_key": "sk-json-secret-value-123456" '
        "postgresql://user:db-password@example.test/db"
    )

    redacted = _redact_text(raw)

    assert jwt not in redacted
    assert "sk-openai-secret-value-123456" not in redacted
    assert "sk-json-secret-value-123456" not in redacted
    assert "db-password" not in redacted
    assert "Bearer <redacted>" in redacted
    assert "OPENAI_API_KEY=<redacted>" in redacted
    assert '"api_key": "<redacted>"' in redacted
    assert "postgresql://user:<redacted>@example.test" in redacted


def test_redacting_filter_applies_to_log_record_args():
    record = logging.LogRecord(
        name="nymeria.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="token=%s",
        args=("sk-ant-abcdefghijklmnopqrstuvwxyz123456",),
        exc_info=None,
    )

    assert _TokenRedactingFilter().filter(record) is True

    assert "abcdefghijklmnopqrstuvwxyz123456" not in record.getMessage()


def test_redact_text_chunks_large_strings():
    secret = "sk-ant-abcdefghijklmnopqrstuvwxyz123456"
    redacted = _redact_text(("x" * 40_000) + secret)

    assert secret not in redacted
    assert "sk-ant...3456" in redacted


def test_redact_text_catches_secret_split_across_chunk_boundary():
    secret = "sk-ant-abcdefghijklmnopqrstuvwxyz123456"
    redacted = _redact_text(("x" * (_REDACTION_CHUNK_SIZE - 8)) + secret)

    assert secret not in redacted
    assert "sk-ant...3456" in redacted


def test_formatter_includes_active_request_id():
    record = logging.LogRecord(
        name="nymeria.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    formatter = NymeriaFormatter(use_color=False)

    token = set_request_id("req-test-123")
    try:
        formatted = formatter.format(record)
    finally:
        reset_request_id(token)

    assert "request_id=req-test-123" in formatted


def test_token_counters_are_not_secrets():
    """`output_tokens=4` is a usage counter on the EMPTY TURN warning line,
    not a credential; the assignment pattern must not eat it while still
    redacting a real `*_TOKEN=` assignment."""
    line = (
        "EMPTY TURN survived retries (output_tokens=4, input_tokens=19855); "
        "LLM_MAX_TOKENS=8192; API_TOKEN=abcdefghijklmnop; ACCESS_TOKENS=qrstuvwxyz0123"
    )
    redacted = _redact_text(line)
    assert "output_tokens=4" in redacted
    assert "input_tokens=19855" in redacted
    assert "LLM_MAX_TOKENS=8192" in redacted
    assert "abcdefghijklmnop" not in redacted
    assert "API_TOKEN=<redacted>" in redacted
    # A plural key whose value is not a number is still a secret.
    assert "qrstuvwxyz0123" not in redacted
