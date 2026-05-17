"""Security regressions for RSS trigger egress."""

from __future__ import annotations

import pytest

from nymeria.core.http_policy import HTTPPolicyViolation
from nymeria.triggers.sources.rss_source import RSSSource


def test_rss_source_blocks_private_feed_url():
    source = RSSSource()

    with pytest.raises(HTTPPolicyViolation):
        source.check({"url": "http://127.0.0.1:8000/feed.xml"}, {})
