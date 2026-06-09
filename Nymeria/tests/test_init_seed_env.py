"""The `.env.docker` init-pick carrier contract (`config/init_seed_env.py`).

The host wizard writes the bootstrap admin's tool/skill picks here; the container
reads them on first boot. This pins the wire format both sides depend on:
`:`-joined identifier lists that write UNQUOTED through `format_env_value`, the
inverse round-trip, and the unset -> None contract the readers rely on.
"""

from __future__ import annotations

from nymeria.config.env_file import format_env_value
from nymeria.config.init_seed_env import (
    INIT_DEFAULT_THREAD_TOOLS_ENV,
    INIT_ENABLED_GLOBAL_SKILLS_ENV,
    format_init_name_list,
    init_default_thread_tools_from_env,
    init_enabled_global_skills_from_env,
    parse_init_name_list,
)


def test_format_parse_round_trip_preserves_order():
    names = ["read_file", "web_search_tavily", "image_gen_gemini", "fetch_url_nymeria"]
    raw = format_init_name_list(names)
    assert raw == "read_file:web_search_tavily:image_gen_gemini:fetch_url_nymeria"
    assert parse_init_name_list(raw) == names


def test_format_dedupes_and_drops_blanks_order_preserving():
    assert format_init_name_list(["a", "a", " b ", "", "c", "b"]) == "a:b:c"
    assert parse_init_name_list("a:a: b ::c:b") == ["a", "b", "c"]


def test_joined_value_writes_unquoted_through_format_env_value():
    # The whole point of `:` over `,`: every char stays in format_env_value's safe
    # set, so the .env.docker line is unquoted and never hits Docker env_file's
    # version-fragile quote handling.
    raw = format_init_name_list(["self-improve", "tool-management", "mcp-management"])
    assert format_env_value(raw) == raw  # unchanged == unquoted


def test_parse_empty_and_none():
    assert parse_init_name_list(None) == []
    assert parse_init_name_list("") == []
    assert parse_init_name_list("   ") == []


def test_readers_return_none_when_unset(monkeypatch):
    monkeypatch.delenv(INIT_DEFAULT_THREAD_TOOLS_ENV, raising=False)
    monkeypatch.delenv(INIT_ENABLED_GLOBAL_SKILLS_ENV, raising=False)
    assert init_default_thread_tools_from_env() is None
    assert init_enabled_global_skills_from_env() is None
    # An explicitly blank var is also "no picks".
    monkeypatch.setenv(INIT_DEFAULT_THREAD_TOOLS_ENV, "")
    assert init_default_thread_tools_from_env() is None


def test_readers_parse_from_os_environ(monkeypatch):
    monkeypatch.setenv(INIT_DEFAULT_THREAD_TOOLS_ENV, "read_file:web_search_brave")
    monkeypatch.setenv(INIT_ENABLED_GLOBAL_SKILLS_ENV, "self-improve:tool-management")
    assert init_default_thread_tools_from_env() == ["read_file", "web_search_brave"]
    assert init_enabled_global_skills_from_env() == ["self-improve", "tool-management"]


def test_readers_accept_an_explicit_mapping():
    env = {INIT_DEFAULT_THREAD_TOOLS_ENV: "a:b:c"}
    assert init_default_thread_tools_from_env(env) == ["a", "b", "c"]
    assert init_enabled_global_skills_from_env(env) is None
