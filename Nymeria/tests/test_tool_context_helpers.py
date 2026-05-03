"""Tests for tools/utils.py runtime-context helpers.

Covers get_user_id, get_thread_id, and get_thread_id_or_none across
normal configs, missing keys, empty strings, None configs, and malformed
configurable dicts.
"""

from __future__ import annotations

import pytest

from nymeria.tools.utils import get_thread_id, get_thread_id_or_none, get_user_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(user_id=None, thread_id=None):
    """Build a minimal RunnableConfig-shaped dict."""
    configurable = {}
    if user_id is not None:
        configurable["user_id"] = user_id
    if thread_id is not None:
        configurable["thread_id"] = thread_id
    return {"configurable": configurable}


# ---------------------------------------------------------------------------
# get_user_id
# ---------------------------------------------------------------------------

class TestGetUserId:
    def test_normal(self):
        assert get_user_id(_cfg(user_id="alice")) == "alice"

    def test_none_config(self):
        assert get_user_id(None) == "default"

    def test_missing_configurable(self):
        assert get_user_id({}) == "default"

    def test_missing_user_id(self):
        assert get_user_id({"configurable": {}}) == "default"

    def test_empty_string_coerced(self):
        assert get_user_id(_cfg(user_id="")) == "default"

    def test_none_value_coerced(self):
        cfg = {"configurable": {"user_id": None}}
        assert get_user_id(cfg) == "default"

    def test_whitespace_preserved(self):
        assert get_user_id(_cfg(user_id=" bob ")) == " bob "


# ---------------------------------------------------------------------------
# get_thread_id
# ---------------------------------------------------------------------------

class TestGetThreadId:
    def test_normal(self):
        assert get_thread_id(_cfg(thread_id="thread-123")) == "thread-123"

    def test_none_config(self):
        assert get_thread_id(None) == "default"

    def test_missing_configurable(self):
        assert get_thread_id({}) == "default"

    def test_missing_thread_id(self):
        assert get_thread_id({"configurable": {}}) == "default"

    def test_empty_string_coerced(self):
        assert get_thread_id(_cfg(thread_id="")) == "default"

    def test_none_value_coerced(self):
        cfg = {"configurable": {"thread_id": None}}
        assert get_thread_id(cfg) == "default"


# ---------------------------------------------------------------------------
# get_thread_id_or_none
# ---------------------------------------------------------------------------

class TestGetThreadIdOrNone:
    def test_normal(self):
        assert get_thread_id_or_none(_cfg(thread_id="thread-456")) == "thread-456"

    def test_none_config(self):
        assert get_thread_id_or_none(None) is None

    def test_missing_configurable(self):
        assert get_thread_id_or_none({}) is None

    def test_missing_thread_id(self):
        assert get_thread_id_or_none({"configurable": {}}) is None

    def test_empty_string_returns_none(self):
        assert get_thread_id_or_none(_cfg(thread_id="")) is None

    def test_none_value_returns_none(self):
        cfg = {"configurable": {"thread_id": None}}
        assert get_thread_id_or_none(cfg) is None

    def test_default_string_preserved(self):
        assert get_thread_id_or_none(_cfg(thread_id="default")) == "default"


# ---------------------------------------------------------------------------
# Cross-function consistency
# ---------------------------------------------------------------------------

class TestConsistency:
    def test_both_ids_from_same_config(self):
        cfg = _cfg(user_id="alice", thread_id="thread-789")
        assert get_user_id(cfg) == "alice"
        assert get_thread_id(cfg) == "thread-789"
        assert get_thread_id_or_none(cfg) == "thread-789"

    def test_empty_config_all_defaults(self):
        cfg: dict = {"configurable": {}}
        assert get_user_id(cfg) == "default"
        assert get_thread_id(cfg) == "default"
        assert get_thread_id_or_none(cfg) is None
