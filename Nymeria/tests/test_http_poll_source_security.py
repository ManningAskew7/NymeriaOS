"""Security regressions for the http_poll trigger egress."""

from __future__ import annotations

import pytest

from nymeria.core.http_policy import HTTPPolicyViolation
from nymeria.triggers.sources.http_poll_source import HTTPPollSource


def test_http_poll_check_blocks_loopback_url():
    source = HTTPPollSource()

    with pytest.raises(HTTPPolicyViolation):
        source.check({"url": "http://127.0.0.1:8000/status"}, {})


def test_http_poll_check_blocks_metadata_ip():
    source = HTTPPollSource()

    with pytest.raises(HTTPPolicyViolation):
        source.check({"url": "http://169.254.169.254/latest/meta-data/"}, {})


def test_http_poll_validate_config_rejects_private_literal():
    source = HTTPPollSource()

    ok, _msg = source.validate_config({"url": "http://10.0.0.5/health"})
    assert ok is False


def test_http_poll_validate_config_allows_public_url():
    source = HTTPPollSource()

    ok, _msg = source.validate_config({"url": "https://api.example.com/status"})
    assert ok is True
