from types import SimpleNamespace

import pytest

from nymeria.core.mcp_tool_names import (
    MAX_MCP_TOOL_NAME_LEN,
    assert_cliproxy_safe,
    display_mcp_tool_names,
    format_mcp_tool_name,
    is_cliproxy_unsafe,
    mcp_auth_status_for_install,
    mcp_tool_display_name,
    parse_mcp_tool_name,
    registered_mcp_tool_names,
    sanitize_tool_component,
)


def test_mcp_tool_name_helpers_keep_internal_and_display_names_separate():
    internal = format_mcp_tool_name("fetch-a1b2c3", "fetch")

    assert internal == "mcp__fetch-a1b2c3__fetch"
    assert parse_mcp_tool_name(internal) == ("fetch-a1b2c3", "fetch")
    assert mcp_tool_display_name(internal, {"fetch-a1b2c3": "Fetch"}) == "Fetch / fetch"


def test_mcp_tool_name_helpers_build_server_tool_lists():
    server = SimpleNamespace(
        id="filesystem-a1b2c3",
        name="Filesystem",
        discovered_tools=[
            SimpleNamespace(name="read_file"),
            SimpleNamespace(name="write_file"),
        ],
    )

    assert registered_mcp_tool_names(server) == [
        "mcp__filesystem-a1b2c3__read_file",
        "mcp__filesystem-a1b2c3__write_file",
    ]
    assert display_mcp_tool_names(server) == [
        "Filesystem / read_file",
        "Filesystem / write_file",
    ]


def test_mcp_tool_name_helpers_accept_mapping_shapes():
    server = {
        "id": "memory-a1b2c3",
        "name": "Memory",
        "discovered_tools": [{"name": "create_entities"}],
    }

    assert registered_mcp_tool_names(server) == [
        "mcp__memory-a1b2c3__create_entities",
    ]
    assert display_mcp_tool_names(server) == [
        "Memory / create_entities",
    ]


def test_mcp_tool_display_label_falls_back_to_tool_name_without_server_label():
    assert display_mcp_tool_names({}, [{"name": "ping"}]) == ["ping"]


# Pinned to the tested classifier table in docs/private/cliproxy.md
# ("Claude OAuth MCP tool-name classifier"). A regression here means a naming
# change would silently 400 every Claude-OAuth thread with an MCP tool bound.
@pytest.mark.parametrize(
    "name,unsafe",
    [
        ("mcp_search", True),
        ("mcp_install", True),
        ("mcp_x", True),
        ("mcp_manage", True),
        ("mcp.search", True),
        ("mcp/search", True),
        ("mcp-server-github", True),  # leading 'mcp-' (hyphen)
        ("manage_mcp", False),
        ("search_mcp", False),
        ("install_mcp_server", False),
        ("mcp__x", False),
        ("mcp__server__tool", False),
    ],
)
def test_is_cliproxy_unsafe_matches_tested_classifier_table(name, unsafe):
    assert is_cliproxy_unsafe(name) is unsafe


def test_format_mcp_tool_name_is_always_cliproxy_safe():
    name = format_mcp_tool_name("mcp-server-github", "list")  # 'mcp'-leading id
    assert name.startswith("mcp__") and not is_cliproxy_unsafe(name)


def test_assert_cliproxy_safe_raises_on_unsafe_name():
    with pytest.raises(ValueError):
        assert_cliproxy_safe("mcp_search")


@pytest.mark.parametrize(
    "install_status,expected",
    [
        ("ready", "connected"),
        ("discovering", "connected"),
        ("needs_config", "needs_setup"),
        ("failed", "needs_setup"),
        ("draft", None),
        ("preparing", None),
        ("disabled", None),
        ("", None),
    ],
)
def test_mcp_auth_status_for_install_maps_setup_axis(install_status, expected):
    assert mcp_auth_status_for_install(install_status) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("search_docs", "search_docs"),   # already safe, unchanged
        ("list-issues", "list-issues"),   # hyphen allowed
        ("search.docs", "search_docs"),   # dot -> underscore (provider 400 fix)
        ("search docs", "search_docs"),   # space -> underscore
        ("créer", "cr_er"),               # unicode -> underscore
        ("a/b:c", "a_b_c"),               # slash/colon -> underscore
        ("", ""),                          # empty stays empty (caller skips it)
    ],
)
def test_sanitize_tool_component_maps_to_provider_charset(raw, expected):
    assert sanitize_tool_component(raw) == expected


def test_format_mcp_tool_name_sanitizes_and_round_trips():
    # A dotted/spaced third-party tool name becomes provider-safe, and the raw
    # server segment is preserved so parse_mcp_tool_name still splits correctly.
    name = format_mcp_tool_name("notion", "search docs")
    assert name == "mcp__notion__search_docs"
    assert not is_cliproxy_unsafe(name)
    assert parse_mcp_tool_name(name) == ("notion", "search_docs")


def test_format_mcp_tool_name_caps_length_truncating_the_tool_component():
    long_tool = "x" * 200
    name = format_mcp_tool_name("srv", long_tool)
    assert len(name) <= MAX_MCP_TOOL_NAME_LEN
    # Server segment intact + still parseable after truncation.
    parsed = parse_mcp_tool_name(name)
    assert parsed is not None and parsed[0] == "srv"
    assert not is_cliproxy_unsafe(name)


def test_format_mcp_tool_name_caps_even_when_server_id_exhausts_budget():
    # Pathological: a server id long enough that the prefix alone exceeds the
    # budget must still never yield an over-length name reaching the provider.
    name = format_mcp_tool_name("s" * 60, "realtool")
    assert len(name) <= MAX_MCP_TOOL_NAME_LEN
    assert name.startswith("mcp__")
    assert not is_cliproxy_unsafe(name)


def test_registered_names_dedupe_and_skip_whitespace():
    server = SimpleNamespace(
        id="srv",
        discovered_tools=[
            SimpleNamespace(name="a.b"),
            SimpleNamespace(name="a b"),   # sanitizes to the same as a.b
            SimpleNamespace(name="   "),   # whitespace-only -> skipped
            SimpleNamespace(name="real"),
        ],
    )
    # Matches what get_all_tools would bind: first of the collision, no blanks.
    assert registered_mcp_tool_names(server) == ["mcp__srv__a_b", "mcp__srv__real"]
