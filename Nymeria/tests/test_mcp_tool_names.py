from types import SimpleNamespace

import pytest

from nymeria.core.mcp_tool_names import (
    assert_cliproxy_safe,
    display_mcp_tool_names,
    format_mcp_tool_name,
    is_cliproxy_unsafe,
    mcp_tool_display_name,
    parse_mcp_tool_name,
    registered_mcp_tool_names,
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
